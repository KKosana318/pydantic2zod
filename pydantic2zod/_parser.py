"""An incomplete Python parser focused around Pydantic declarations."""

import inspect
import logging
from importlib import import_module
from importlib.util import resolve_name
from itertools import chain
from pathlib import Path
from types import ModuleType
from typing import Generic, Literal, NewType, TypeVar, cast

import libcst as cst
import libcst.matchers as m
from networkx import DiGraph, dfs_postorder_nodes
from typing_extensions import Self

from ._model import (
    BuiltinType,
    ClassDecl,
    ClassField,
    GenericType,
    LiteralType,
    PrimitiveType,
    PyDict,
    PyNone,
    PyString,
    PyType,
    PyValue,
    TupleType,
    UnionType,
    UserDefinedType,
)

_logger = logging.getLogger(__name__)

Imports = NewType("Imports", dict[str, str])
"""imported_symbol -> from_module

e.g. Request --> scanner_common.http.cassette
"""


def parse(module: ModuleType) -> list[ClassDecl]:
    model_graph = DiGraph()
    pydantic_models = _parse(module, set(), model_graph)
    models_by_name = {c.name: c for c in pydantic_models}
    ordered_models = list[str](dfs_postorder_nodes(model_graph))
    return [models_by_name[c] for c in ordered_models if c in models_by_name]


def _parse(
    module: ModuleType, parse_only_models: set[str], model_graph: DiGraph
) -> list[ClassDecl]:
    fname = module.__file__ or "SHOULD EXIST"
    _logger.info("Parsing module '%s'", fname)

    classes = list[ClassDecl]()

    parse_module = _ParseModule(module, model_graph, parse_only_models)
    m = cst.parse_module(Path(fname).read_text())
    classes += parse_module.visit(m).classes()

    if depends_on := parse_module.external_models():
        _logger.info("'%s' depends on other pydantic models:", fname)
        for model_path in depends_on:
            _logger.info("    '%s'", model_path)

        for model_path in depends_on:
            m = import_module(".".join(model_path.split(".")[:-1]))
            model_name = model_path.split(".")[-1]
            classes += _parse(m, {model_name}, model_graph)

    return classes


_NodeT = TypeVar("_NodeT", bound=cst.CSTNode)


class _Parse(m.MatcherDecoratableVisitor, Generic[_NodeT]):
    def visit(self, node: _NodeT) -> Self:
        node.visit(self)
        return self


class _ParseModule(_Parse[cst.Module]):
    def __init__(
        self,
        module: ModuleType,
        model_graph: DiGraph,
        parse_only_models: set[str] | None = None,
    ) -> None:
        super().__init__()

        self._parse_only_models = parse_only_models
        self._model_graph = model_graph
        self._parsing_module = module

        self._pydantic_classes: dict[str, ClassDecl] = {}
        self._classes: dict[str, ClassDecl] = {}
        self._class_nodes: dict[str, cst.ClassDef] = {}
        self._alias_nodes: dict[str, cst.AnnAssign] = {}
        self._external_models = set[str]()
        self._imports = Imports({})

    def exec(self) -> Self:
        """A helper for tests."""
        self.visit(cst.parse_module(inspect.getsource(self._parsing_module)))
        return self

    def external_models(self) -> set[str]:
        """A List of pydantic models coming from other Python modules.

        Built-in common types like uuid.UUID are filtered out so that pydanitc2zod
        would not try to parse them recursively.
        """
        ext_models = set[str]()
        for name in self._external_models:
            from_module = self._imports[name]
            abs_module_name = resolve_name(
                from_module, self._parsing_module.__package__
            )
            abs_cls_name = f"{abs_module_name}.{name}"
            if abs_cls_name not in ["uuid.UUID", "pydantic.BaseModel"]:
                ext_models.add(abs_cls_name)

        return ext_models

    def classes(self) -> list[ClassDecl]:
        ordered_models = list(dfs_postorder_nodes(self._model_graph))
        return [
            self._pydantic_classes[c]
            for c in ordered_models
            if c in self._pydantic_classes
        ]

    def visit_ImportFrom(self, node: cst.ImportFrom):
        self._imports |= _ParseImportFrom().visit(node).imports()

    def visit_ClassDef(self, node: cst.ClassDef):
        parse = _ParseClassDecl()
        parse.visit_ClassDef(node)
        cls = parse.class_decl

        self._class_nodes[cls.name] = node
        self._classes[cls.name] = cls
        # TODO(povilas): add_node(cls.full_path) - http.cassette.Request
        self._model_graph.add_node(cls.name)

    @m.call_if_inside(
        m.AnnAssign(annotation=m.Annotation(annotation=m.Name("TypeAlias")))
    )
    # Only global namespace.
    @m.call_if_not_inside(m.AllOf(m.ClassDef(), m.FunctionDef()))
    def visit_AnnAssign(self, node: cst.AnnAssign):
        target = cst.ensure_type(node.target, cst.Name).value
        # We will parse the alias declaration lazily when one is used within a pydantic
        # model.
        self._alias_nodes[target] = node

    def leave_Module(self, original_node: cst.Module) -> None:
        """Parse the class definitions and resolve imported classes."""
        if self._parse_only_models:
            for m in self._parse_only_models:
                self._parse_pydantic_model(self._classes[m])
        else:
            self._parse_all_classes()
            for cls in self._pydantic_classes.values():
                for dep in self._class_deps(cls):
                    self._model_graph.add_edge(cls.name, dep)
                    if dep in self._imports:
                        self._external_models.add(dep)
                    elif dep not in self._classes:
                        _logger.warning(
                            "Can't infer where '%s' is coming from. '%s' depends on it.",
                            dep,
                            cls.name,
                        )

    def _parse_pydantic_model(self, cls: ClassDecl) -> None:
        """Parse a Pydantic model."""
        if not self._is_pydantic_model(cls) or cls.name in self._pydantic_classes:
            return None

        cls = self._finish_parsing_class(cls)
        for dep in self._class_deps(cls):
            self._model_graph.add_edge(cls.name, dep)
            if dep in self._imports:
                self._external_models.add(dep)
            elif cls_decl := self._classes.get(dep):
                self._parse_pydantic_model(cls_decl)
            else:
                _logger.warning(
                    "Can't infer where '%s' is coming from. '%s' depends on it.",
                    dep,
                    cls.name,
                )

    def _class_deps(self, cls: ClassDecl) -> list[str]:
        deps = [c for c in cls.base_classes if c != "BaseModel"]
        for f in cls.fields:
            for type_ in _get_user_defined_types(f.type):
                deps.append(type_)
        return deps

    def _parse_all_classes(self) -> None:
        """This case is easier as we traverse classes in a linear order parsing one by
        one."""
        for cls_decl in self._classes.values():
            if self._is_pydantic_model(cls_decl):
                self._finish_parsing_class(cls_decl)

    def _finish_parsing_class(self, cls_decl: ClassDecl) -> ClassDecl:
        cls = _ParseClassDecl().visit(self._class_nodes[cls_decl.name]).class_decl
        self._pydantic_classes[cls.name] = cls
        self._model_graph.add_node(cls.name)

        # Try to resolve type aliases.
        for f in cls.fields:
            if isinstance(f.type, UserDefinedType):
                if node := self._alias_nodes.get(f.type.name):
                    assert node.value
                    f.type = _extract_type(node.value)

        return cls

    def _is_pydantic_model(self, cls: ClassDecl) -> bool:
        if "BaseModel" in cls.base_classes and self._imports["BaseModel"] == "pydantic":
            return True

        # TODO(povilas): when the base is imported model

        for b in cls.base_classes:
            if b in self._classes:
                return self._is_pydantic_model(self._classes[b])

        return False


class _ParseClassDecl(_Parse[cst.ClassDef]):
    def __init__(self) -> None:
        super().__init__()
        self.class_decl = ClassDecl(name="to_be_parsed", base_classes=[])
        self._last_field_nr = 0

    def visit_ClassDef(self, node: cst.ClassDef):
        base_classes = [
            b.value.value for b in node.bases if isinstance(b.value, cst.Name)
        ]
        self.class_decl = ClassDecl(name=node.name.value, base_classes=base_classes)

    @m.call_if_inside(m.ClassDef())
    @m.call_if_not_inside(m.FunctionDef())
    @m.call_if_inside(m.SimpleStatementLine(body=[m.AtMostN(m.Expr(), n=1)]))
    def visit_SimpleString(self, node: cst.SimpleString):
        comment = node.value.replace('"""', "")

        if not self._last_field_nr:
            self.class_decl.comment = comment
        else:
            self.class_decl.fields[self._last_field_nr - 1].comment = comment

    @m.call_if_inside(m.ClassDef())
    @m.call_if_not_inside(m.FunctionDef())
    def visit_AnnAssign(self, node: cst.AnnAssign):
        self._last_field_nr += 1

        target = cst.ensure_type(node.target, cst.Name).value
        type_ = _extract_type(node.annotation.annotation)
        default_value = _parse_value(node.value) if node.value else None
        self.class_decl.fields.append(
            ClassField(name=target, type=type_, default_value=default_value),
        )


class _ParseImportFrom(_Parse[cst.ImportFrom]):
    def __init__(self) -> None:
        super().__init__()
        self._from = list[str]()
        self._imports = list[str]()
        self._relative = 0

    def imports(self) -> dict[str, str]:
        from_ = "." * self._relative + ".".join(self._from)
        return {imp: from_ for imp in self._imports}

    def visit_ImportFrom(self, node: cst.ImportFrom):
        self._relative = len(list(node.relative))

    @m.call_if_not_inside(m.ImportAlias())
    def visit_Name(self, node: cst.Name):
        self._from.append(node.value)

    def visit_ImportAlias(self, node: cst.ImportAlias) -> None:
        self._imports.append(cst.ensure_type(node.name, cst.Name).value)


def _extract_type(node: cst.BaseExpression) -> PyType:
    match node:
        case cst.Name(value=type_name):
            return _primitive_or_user_defined_type(type_name)
        case cst.Subscript():
            return _parse_generic_type(node)
        case cst.BinaryOperation():
            return _extract_union(node)
        case _:
            assert False, f"Unexpected node in type definition: '{node.__class__}'"


def _get_user_defined_types(tp: PyType) -> list[str]:
    match tp:
        case UserDefinedType(name=name):
            return [name]
        case UnionType(types=types):
            return list(chain(*[_get_user_defined_types(t) for t in types]))
        case LiteralType(type=type_):
            return _get_user_defined_types(type_)
        case GenericType(type_vars=args):
            return list(chain(*[_get_user_defined_types(a) for a in args]))
        case _:
            return []


def _parse_generic_type(
    node: cst.Subscript,
) -> GenericType | LiteralType | UnionType | TupleType:
    generic_type = cst.ensure_type(node.value, cst.Name).value
    match generic_type:
        case "Literal":
            return _parse_literal(node)
        case "list" | "List":
            return GenericType(generic="list", type_vars=_parse_types_list(node))
        case "dict" | "Dict":
            return GenericType(generic="dict", type_vars=_parse_types_list(node))
        case "Union":
            return UnionType(types=_parse_types_list(node))
        case "Optional":
            return UnionType(
                types=_parse_types_list(node) + [PrimitiveType(name="None")]
            )
        case "tuple" | "Tuple":
            return TupleType(types=_parse_types_list(node))
        case other:
            assert False, f"Unexpected generic type: '{other}'"


def _parse_literal(node: cst.Subscript) -> LiteralType | UnionType:
    assert cst.ensure_type(node.value, cst.Name).value == "Literal"

    literal_values = []
    for elem in node.slice:
        value = cst.ensure_type(
            cst.ensure_type(elem.slice, cst.Index).value, cst.SimpleString
        ).value.replace('"', "")
        literal_values.append(value)

    if len(literal_values) == 1:
        return LiteralType(value=literal_values[0])
    else:
        return UnionType(types=[LiteralType(value=v) for v in literal_values])


def _parse_types_list(node: cst.Subscript) -> list[PyType]:
    types = list[PyType]()
    for element in node.slice:
        type_var_node = cst.ensure_type(element.slice, cst.Index).value
        match type_var_node:
            case cst.Name(value=type_var):
                types.append(_primitive_or_user_defined_type(type_var))
            case other:
                types.append(_extract_type(other))
    return types


def _primitive_or_user_defined_type(
    type_name: str,
) -> PrimitiveType | UserDefinedType | BuiltinType:
    match type_name:
        case "str" | "bytes" | "bool" | "int" | "float" | "None":
            return PrimitiveType(name=type_name)
        case "dict" | "Dict" | "list" | "List":
            return BuiltinType(name=cast(Literal["dict", "list"], type_name.lower()))
        case _:
            return UserDefinedType(name=type_name)


def _extract_union(node: cst.BinaryOperation) -> UnionType:
    cst.ensure_type(node.operator, cst.BitOr)
    all_types = []

    left = _extract_type(node.left)
    match left:
        case UnionType(types=types):
            all_types += types
        case single_type:
            all_types.append(single_type)

    right = _extract_type(node.right)
    match right:
        case UnionType(types=types):
            all_types += types
        case single_type:
            all_types.append(single_type)

    return UnionType(types=all_types)


def _parse_value(node: cst.BaseExpression) -> PyValue:
    match node:
        case cst.SimpleString(value=value):
            return PyString(value=value.replace('"', ""))
        case cst.Name(value="None"):
            return PyNone()
        case cst.Dict():
            return PyDict()
        case other:
            _logger.warning("Unsupported value type: '%s'", other)
            return PyNone()


# TODO(povilas): consider making Imports a class
def _resolve_path(import_: str, from_: str) -> str:
    return f"{from_}.{import_}"
