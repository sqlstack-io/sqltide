//! SQLTide's frozen data shapes, value enums and exceptions, defined natively.
//!
//! Each shape is a frozen class that keeps the public module path, fields, constructor, equality,
//! hashing, repr and `dataclasses` protocol of the frozen dataclass it replaces, so
//! `dataclasses.replace`, `fields` and `asdict` keep working on it. Enums are real `StrEnum`s and
//! exceptions are ordinary exception classes, both built when the native module loads.

use pyo3::exceptions::{PyAttributeError, PyBaseException, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::pyclass::CompareOp;
use pyo3::sync::PyOnceLock;
use pyo3::types::{IntoPyDict, PyDict, PyFrozenSet, PyString, PyTuple, PyType};

mod compiler_analysis;

/// A field default, as plain data so each shape's field table is a constant.
#[derive(Clone, Copy)]
pub(crate) enum Given {
    Required,
    None,
    Bool(bool),
    Int(i64),
    Str(&'static str),
    EmptyTuple,
    /// A member of a native enum, by its `sqltide._native` attribute and member name.
    Member(&'static str, &'static str),
}

/// How a constructor argument is normalized, as the dataclass's `__post_init__` did.
#[derive(Clone, Copy)]
pub(crate) enum Coerce {
    Keep,
    /// Call the native enum registered as this `sqltide._native` attribute with the value.
    Enum(&'static str),
    /// Call the class at `module.name`, for an enum that is still defined in Python.
    Class(&'static str, &'static str),
    /// Freeze a mapping as `MappingProxyType(dict(value))`.
    ReadOnlyMapping,
}

/// One dataclass field: its name, default, normalization and `compare`/`repr` flags.
#[derive(Clone, Copy)]
pub(crate) struct FieldSpec {
    pub(crate) name: &'static str,
    pub(crate) default: Given,
    pub(crate) coerce: Coerce,
    pub(crate) compare: bool,
    pub(crate) repr: bool,
}

impl FieldSpec {
    pub(crate) const BASE: Self = Self {
        name: "",
        default: Given::Required,
        coerce: Coerce::Keep,
        compare: true,
        repr: true,
    };
}

/// One shape: its class name and fields in declaration order.
pub(crate) struct ShapeSpec {
    pub(crate) qualname: &'static str,
    pub(crate) fields: &'static [FieldSpec],
}

/// The Python name of a Rust field identifier, without a raw-identifier prefix.
pub(crate) const fn unraw(name: &'static str) -> &'static str {
    match name.as_bytes() {
        [b'r', b'#', rest @ ..] => match std::str::from_utf8(rest) {
            Ok(rest) => rest,
            Err(_) => name,
        },
        _ => name,
    }
}

/// Defines frozen shapes under one public module path. Each shape is written as
/// `Name { field, field(default = Given::None), ... }`, optionally followed by
/// `@impl { ... }` holding its extra methods and properties.
macro_rules! shapes {
    (module = $module:literal; $(
        $(#[doc = $doc:literal])*
        $name:ident {
            $($field:ident $(($($key:ident = $value:expr),* $(,)?))?),* $(,)?
        }
        $(@impl { $($methods:tt)* })?
    )*) => { $(
        $(#[doc = $doc])*
        #[pyclass(module = $module, frozen, get_all)]
        pub(crate) struct $name {
            $(pub(crate) $field: pyo3::Py<pyo3::PyAny>,)*
        }

        impl $name {
            pub(crate) const SPEC: $crate::shapes::ShapeSpec = $crate::shapes::ShapeSpec {
                qualname: stringify!($name),
                fields: &[$($crate::shapes::FieldSpec {
                    name: $crate::shapes::unraw(stringify!($field)),
                    $($($key: $value,)*)?
                    ..$crate::shapes::FieldSpec::BASE
                }),*],
            };

            fn values<'py>(&self, py: pyo3::Python<'py>) -> Vec<pyo3::Bound<'py, pyo3::PyAny>> {
                vec![$(self.$field.bind(py).clone()),*]
            }
        }

        #[pymethods]
        impl $name {
            #[new]
            #[pyo3(signature = (*args, **kwargs))]
            fn __new__(
                args: &pyo3::Bound<'_, pyo3::types::PyTuple>,
                kwargs: Option<&pyo3::Bound<'_, pyo3::types::PyDict>>,
            ) -> pyo3::PyResult<Self> {
                let mut values = $crate::shapes::bind(&Self::SPEC, args, kwargs)?.into_iter();
                Ok(Self { $($field: $crate::shapes::next(&mut values)?,)* })
            }

            #[classattr]
            fn __match_args__(
                py: pyo3::Python<'_>,
            ) -> pyo3::PyResult<pyo3::Py<pyo3::types::PyTuple>> {
                $crate::shapes::match_args(py, &Self::SPEC)
            }

            #[classattr]
            fn __dataclass_fields__(
                py: pyo3::Python<'_>,
            ) -> pyo3::PyResult<pyo3::Py<pyo3::types::PyDict>> {
                $crate::shapes::dataclass_fields(py, &Self::SPEC)
            }

            #[classattr]
            fn __dataclass_params__(py: pyo3::Python<'_>) -> pyo3::PyResult<pyo3::Py<pyo3::PyAny>> {
                $crate::shapes::dataclass_params(py)
            }

            fn __repr__(&self, py: pyo3::Python<'_>) -> pyo3::PyResult<String> {
                $crate::shapes::repr(&Self::SPEC, &self.values(py))
            }

            fn __richcmp__(
                slf: &pyo3::Bound<'_, Self>,
                other: &pyo3::Bound<'_, pyo3::PyAny>,
                op: pyo3::pyclass::CompareOp,
            ) -> pyo3::PyResult<pyo3::Py<pyo3::PyAny>> {
                let py: pyo3::Python<'_> = slf.py();
                match other.cast::<Self>() {
                    Ok(other) if other.get_type().is(&slf.get_type()) => $crate::shapes::compare(
                        py,
                        &Self::SPEC,
                        &slf.get().values(py),
                        &other.get().values(py),
                        op,
                    ),
                    _ => Ok(py.NotImplemented()),
                }
            }

            fn __hash__(&self, py: pyo3::Python<'_>) -> pyo3::PyResult<isize> {
                $crate::shapes::compared(py, &Self::SPEC, &self.values(py))?.hash()
            }

            fn __setattr__(
                &self,
                py: pyo3::Python<'_>,
                name: &str,
                _value: &pyo3::Bound<'_, pyo3::PyAny>,
            ) -> pyo3::PyResult<()> {
                Err($crate::shapes::frozen(py, "assign to", name))
            }

            fn __delattr__(&self, py: pyo3::Python<'_>, name: &str) -> pyo3::PyResult<()> {
                Err($crate::shapes::frozen(py, "delete", name))
            }

            fn __getattr__(
                slf: &pyo3::Bound<'_, Self>,
                name: &pyo3::Bound<'_, pyo3::types::PyString>,
            ) -> pyo3::PyResult<pyo3::Py<pyo3::PyAny>> {
                Err($crate::shapes::no_attribute(slf.as_any(), Self::SPEC.qualname, name))
            }

            fn __traverse__(&self, visit: pyo3::PyVisit<'_>) -> Result<(), pyo3::PyTraverseError> {
                $(visit.call(&self.$field)?;)*
                Ok(())
            }

            $($($methods)*)?
        }
    )* };
}
pub(crate) use shapes;

/// The next bound constructor value, in field order.
pub(crate) fn next<'py>(
    values: &mut impl Iterator<Item = Bound<'py, PyAny>>,
) -> PyResult<Py<PyAny>> {
    values
        .next()
        .map(Bound::unbind)
        .ok_or_else(|| PyValueError::new_err("shape constructor bound too few values"))
}

/// Native enums by their `sqltide._native` attribute, for defaults and coercions.
static ENUMS: PyOnceLock<Py<PyDict>> = PyOnceLock::new();

fn native_enum<'py>(py: Python<'py>, attribute: &str) -> PyResult<Bound<'py, PyAny>> {
    ENUMS
        .get_or_init(py, || PyDict::new(py).unbind())
        .bind(py)
        .get_item(attribute)?
        .ok_or_else(|| PyValueError::new_err(format!("native enum {attribute} is not built")))
}

fn plural(count: usize) -> &'static str {
    if count == 1 { "" } else { "s" }
}

/// Quoted names joined as CPython lists missing arguments: `'a'`, `'a' and 'b'`, `'a', 'b', and 'c'`.
fn missing_list(names: &[&str]) -> String {
    let quoted: Vec<String> = names.iter().map(|name| format!("'{name}'")).collect();
    match quoted.as_slice() {
        [] => String::new(),
        [one] => one.clone(),
        [first, second] => format!("{first} and {second}"),
        [init @ .., last] => format!("{}, and {last}", init.join(", ")),
    }
}

/// Binds positional and keyword arguments to the shape's fields as the dataclass `__init__` did,
/// with the same `TypeError` messages, then fills defaults and applies each field's normalization.
pub(crate) fn bind<'py>(
    spec: &ShapeSpec,
    args: &Bound<'py, PyTuple>,
    kwargs: Option<&Bound<'py, PyDict>>,
) -> PyResult<Vec<Bound<'py, PyAny>>> {
    let py: Python<'py> = args.py();
    let function: String = format!("{}.__init__()", spec.qualname);
    let total: usize = spec.fields.len();
    if args.len() > total {
        let required: usize = spec
            .fields
            .iter()
            .filter(|field| matches!(field.default, Given::Required))
            .count();
        let accepted: String = if required == total {
            format!("{} positional argument{}", total + 1, plural(total + 1))
        } else {
            format!(
                "from {} to {} positional arguments",
                required + 1,
                total + 1
            )
        };
        let given: usize = args.len() + 1;
        let verb: &str = if given == 1 { "was" } else { "were" };
        return Err(PyTypeError::new_err(format!(
            "{function} takes {accepted} but {given} {verb} given"
        )));
    }
    let mut slots: Vec<Option<Bound<'py, PyAny>>> = args.iter().map(Some).collect();
    slots.resize(total, None);
    if let Some(kwargs) = kwargs {
        for (key, value) in kwargs.iter() {
            let key: Bound<'py, PyString> = key.cast_into()?;
            let key: &str = key.to_str()?;
            let Some(index) = spec.fields.iter().position(|field| field.name == key) else {
                return Err(PyTypeError::new_err(format!(
                    "{function} got an unexpected keyword argument '{key}'"
                )));
            };
            if slots[index].is_some() {
                return Err(PyTypeError::new_err(format!(
                    "{function} got multiple values for argument '{key}'"
                )));
            }
            slots[index] = Some(value);
        }
    }
    let missing: Vec<&str> = spec
        .fields
        .iter()
        .zip(&slots)
        .filter(|(field, slot)| slot.is_none() && matches!(field.default, Given::Required))
        .map(|(field, _)| field.name)
        .collect();
    if !missing.is_empty() {
        return Err(PyTypeError::new_err(format!(
            "{function} missing {} required positional argument{}: {}",
            missing.len(),
            plural(missing.len()),
            missing_list(&missing)
        )));
    }
    let mut values: Vec<Bound<'py, PyAny>> = Vec::with_capacity(total);
    for (field, slot) in spec.fields.iter().zip(slots) {
        let value: Bound<'py, PyAny> = match slot {
            Some(value) => value,
            None => default_value(py, field.default)?
                .ok_or_else(|| PyValueError::new_err("required field has no value"))?,
        };
        values.push(value);
    }
    for (field, value) in spec.fields.iter().zip(values.iter_mut()) {
        *value = coerce(py, field.coerce, value)?;
    }
    Ok(values)
}

fn default_value(py: Python<'_>, given: Given) -> PyResult<Option<Bound<'_, PyAny>>> {
    Ok(Some(match given {
        Given::Required => return Ok(None),
        Given::None => py.None().into_bound(py),
        Given::Bool(value) => value.into_pyobject(py)?.to_owned().into_any(),
        Given::Int(value) => value.into_pyobject(py)?.into_any(),
        Given::Str(value) => PyString::new(py, value).into_any(),
        Given::EmptyTuple => PyTuple::empty(py).into_any(),
        Given::Member(class, member) => native_enum(py, class)?.getattr(member)?,
    }))
}

fn coerce<'py>(
    py: Python<'py>,
    coerce: Coerce,
    value: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    match coerce {
        Coerce::Keep => Ok(value.clone()),
        Coerce::Enum(class) => native_enum(py, class)?.call1((value,)),
        Coerce::Class(module, name) => py.import(module)?.getattr(name)?.call1((value,)),
        Coerce::ReadOnlyMapping => py
            .import("types")?
            .getattr("MappingProxyType")?
            .call1((py.get_type::<PyDict>().call1((value,))?,)),
    }
}

/// The field names in declaration order, as `__match_args__`.
pub(crate) fn match_args(py: Python<'_>, spec: &ShapeSpec) -> PyResult<Py<PyTuple>> {
    Ok(PyTuple::new(py, spec.fields.iter().map(|field| field.name))?.unbind())
}

/// A frozen dataclass's `Field` template and `__dataclass_params__`, from one probe class.
static PROBE: PyOnceLock<(Py<PyAny>, Py<PyAny>)> = PyOnceLock::new();

fn probe(py: Python<'_>) -> PyResult<&(Py<PyAny>, Py<PyAny>)> {
    PROBE.get_or_try_init(py, || {
        let probe: Bound<'_, PyAny> = py.import("dataclasses")?.getattr("make_dataclass")?.call(
            ("Probe", [("value", "object")]),
            Some(&[("frozen", true)].into_py_dict(py)?),
        )?;
        Ok::<_, PyErr>((
            probe
                .getattr("__dataclass_fields__")?
                .get_item("value")?
                .unbind(),
            probe.getattr("__dataclass_params__")?.unbind(),
        ))
    })
}

/// The `__dataclass_fields__` table that `dataclasses.fields`, `replace` and `asdict` read.
pub(crate) fn dataclass_fields(py: Python<'_>, spec: &ShapeSpec) -> PyResult<Py<PyDict>> {
    let template: &Bound<'_, PyAny> = probe(py)?.0.bind(py);
    let copy: Bound<'_, PyAny> = py.import("copy")?.getattr("copy")?;
    let table: Bound<'_, PyDict> = PyDict::new(py);
    for field in spec.fields {
        let entry: Bound<'_, PyAny> = copy.call1((template,))?;
        entry.setattr("name", field.name)?;
        if let Some(value) = default_value(py, field.default)? {
            entry.setattr("default", value)?;
        }
        entry.setattr("compare", field.compare)?;
        entry.setattr("repr", field.repr)?;
        table.set_item(field.name, entry)?;
    }
    Ok(table.unbind())
}

/// The frozen dataclass parameters shared by every shape.
pub(crate) fn dataclass_params(py: Python<'_>) -> PyResult<Py<PyAny>> {
    Ok(probe(py)?.1.clone_ref(py))
}

/// `Name(field=value, ...)` over the fields shown in repr, as the dataclass repr reads.
pub(crate) fn repr(spec: &ShapeSpec, values: &[Bound<'_, PyAny>]) -> PyResult<String> {
    let mut parts: Vec<String> = Vec::with_capacity(values.len());
    for (field, value) in spec.fields.iter().zip(values) {
        if field.repr {
            parts.push(format!("{}={}", field.name, value.repr()?.to_str()?));
        }
    }
    Ok(format!("{}({})", spec.qualname, parts.join(", ")))
}

/// The tuple of compared fields that the dataclass compares and hashes.
pub(crate) fn compared<'py>(
    py: Python<'py>,
    spec: &ShapeSpec,
    values: &[Bound<'py, PyAny>],
) -> PyResult<Bound<'py, PyTuple>> {
    let compared: Vec<&Bound<'py, PyAny>> = spec
        .fields
        .iter()
        .zip(values)
        .filter(|(field, _)| field.compare)
        .map(|(_, value)| value)
        .collect();
    PyTuple::new(py, compared)
}

/// `==` and `!=` as the dataclass compares: the tuples of compared fields. Ordering is
/// unsupported, as for a dataclass without `order=True`.
pub(crate) fn compare(
    py: Python<'_>,
    spec: &ShapeSpec,
    left: &[Bound<'_, PyAny>],
    right: &[Bound<'_, PyAny>],
    op: CompareOp,
) -> PyResult<Py<PyAny>> {
    if !matches!(op, CompareOp::Eq | CompareOp::Ne) {
        return Ok(py.NotImplemented());
    }
    let equal: bool = compared(py, spec, left)?.eq(compared(py, spec, right)?)?;
    let result: bool = equal == matches!(op, CompareOp::Eq);
    Ok(result.into_pyobject(py)?.to_owned().into_any().unbind())
}

/// `dataclasses.FrozenInstanceError` for assigning or deleting `name`.
pub(crate) fn frozen(py: Python<'_>, action: &str, name: &str) -> PyErr {
    let message: String = format!("cannot {action} field '{name}'");
    match py
        .import("dataclasses")
        .and_then(|module| module.getattr("FrozenInstanceError"))
        .and_then(|class| class.call1((message,)))
    {
        Ok(error) => PyErr::from_value(error),
        Err(error) => error,
    }
}

/// The `AttributeError` CPython raises for a missing attribute, naming the bare class as it did
/// for the dataclass, with `name` and `obj` set so tracebacks still suggest close field names.
pub(crate) fn no_attribute(
    instance: &Bound<'_, PyAny>,
    qualname: &str,
    name: &Bound<'_, PyString>,
) -> PyErr {
    let py: Python<'_> = instance.py();
    let message: String = format!("'{qualname}' object has no attribute '{name}'");
    let error = || -> PyResult<Bound<'_, PyAny>> {
        let keywords: Bound<'_, PyDict> =
            [("name", name.as_any()), ("obj", instance)].into_py_dict(py)?;
        py.get_type::<PyAttributeError>()
            .call((message,), Some(&keywords))
    };
    match error() {
        Ok(error) => PyErr::from_value(error),
        Err(error) => error,
    }
}

/// Builds the `StrEnum` named `qualname` under the public module `public_module` and registers
/// it in `sqltide._native` as `attribute`.
pub(crate) fn str_enum(
    module: &Bound<'_, PyModule>,
    attribute: &'static str,
    public_module: &str,
    qualname: &str,
    doc: Option<&str>,
    members: &[(&str, &str)],
) -> PyResult<()> {
    let py: Python<'_> = module.py();
    let class: Bound<'_, PyAny> = py.import("enum")?.getattr("StrEnum")?.call(
        (qualname, members.to_vec()),
        Some(&[("module", public_module), ("qualname", qualname)].into_py_dict(py)?),
    )?;
    class.setattr("__doc__", doc)?;
    ENUMS
        .get_or_init(py, || PyDict::new(py).unbind())
        .bind(py)
        .set_item(attribute, &class)?;
    module.add(attribute, class)
}

/// Builds the exception class `qualname` under the public module `public_module`, with `bases`
/// and, when given, a native `__init__` that receives the instance first, and registers it in
/// `sqltide._native` under the same name.
pub(crate) fn exception<'py>(
    module: &Bound<'py, PyModule>,
    public_module: &str,
    qualname: &str,
    doc: &str,
    bases: &[Bound<'py, PyAny>],
    init: Option<Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = module.py();
    let namespace: Bound<'py, PyDict> = PyDict::new(py);
    namespace.set_item("__module__", public_module)?;
    namespace.set_item("__qualname__", qualname)?;
    namespace.set_item("__doc__", doc)?;
    if let Some(init) = init {
        let method: Bound<'py, PyAny> = py
            .import("functools")?
            .getattr("partialmethod")?
            .call1((init,))?;
        namespace.set_item("__init__", method)?;
    }
    let class: Bound<'py, PyAny> =
        py.get_type::<PyType>()
            .call1((qualname, PyTuple::new(py, bases)?, namespace))?;
    module.add(qualname, &class)?;
    Ok(class)
}

/// `BaseException.__init__(instance, *args)`, which sets `.args` as `super().__init__` did.
pub(crate) fn base_init<'py>(
    instance: &Bound<'py, PyAny>,
    args: impl IntoIterator<Item = Bound<'py, PyAny>>,
) -> PyResult<()> {
    let py: Python<'py> = instance.py();
    let mut call: Vec<Bound<'py, PyAny>> = vec![instance.clone()];
    call.extend(args);
    py.get_type::<PyBaseException>()
        .getattr("__init__")?
        .call1(PyTuple::new(py, call)?)?;
    Ok(())
}

/// `frozenset` of every member of the native enum registered as `attribute`.
pub(crate) fn all_members<'py>(
    py: Python<'py>,
    attribute: &str,
) -> PyResult<Bound<'py, PyFrozenSet>> {
    Ok(py
        .get_type::<PyFrozenSet>()
        .call1((native_enum(py, attribute)?,))?
        .cast_into()?)
}

/// `frozenset` of the named members of the native enum registered as `attribute`.
pub(crate) fn member_set<'py>(
    py: Python<'py>,
    attribute: &str,
    members: &[&str],
) -> PyResult<Bound<'py, PyFrozenSet>> {
    let class: Bound<'py, PyAny> = native_enum(py, attribute)?;
    let mut items: Vec<Bound<'py, PyAny>> = Vec::with_capacity(members.len());
    for member in members {
        items.push(class.getattr(*member)?);
    }
    PyFrozenSet::new(py, &items)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    compiler_analysis::register(module)
}
