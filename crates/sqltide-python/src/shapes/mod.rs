//! SQLTide's frozen data shapes, value enums and exceptions, defined natively.
//!
//! Each shape is a frozen class that keeps the public module path, fields, constructor, equality,
//! hashing, repr and `dataclasses` protocol of the frozen dataclass it replaces, so
//! `dataclasses.replace`, `fields` and `asdict` keep working on it. Enums are real `StrEnum`s and
//! exceptions are ordinary exception classes, both built when the native module loads.

use pyo3::exceptions::{
    PyAttributeError, PyBaseException, PyImportError, PyTypeError, PyValueError,
};
use pyo3::prelude::*;
use pyo3::pyclass::CompareOp;
use pyo3::sync::PyOnceLock;
use pyo3::types::{
    IntoPyDict, PyCFunction, PyDict, PyFloat, PyFrozenSet, PyString, PyTuple, PyType,
};

mod adapter_models;
mod cli_service;
mod compiler_analysis;
mod compiler_project;
mod discovery_models;
mod executor_ops;
mod planner_execution;
mod sensors_dev;

/// The callable a `default_factory` field calls for each new instance.
pub(crate) type Factory = for<'py> fn(Python<'py>) -> PyResult<Bound<'py, PyAny>>;

/// A field default, as plain data so each shape's field table is a constant.
#[derive(Clone, Copy)]
pub(crate) enum Given {
    Required,
    None,
    Bool(bool),
    Int(i64),
    Float(f64),
    Str(&'static str),
    EmptyTuple,
    EmptyFrozenSet,
    /// A member of a native enum, by its `sqltide._native` attribute and member name.
    Member(&'static str, &'static str),
    /// `field(default_factory=...)`: a fresh value from calling the factory's callable.
    Factory(Factory),
    /// A shared default built on first use, for a value whose class is still defined in Python.
    Lazy(Factory),
}

/// How a constructor argument is normalized, as the dataclass's `__post_init__` did.
#[derive(Clone, Copy)]
pub(crate) enum Coerce {
    Keep,
    /// Call the native enum registered as this `sqltide._native` attribute with the value.
    Enum(&'static str),
    /// Freeze a mapping as `MappingProxyType(dict(value))`.
    ReadOnlyMapping,
    /// `tuple(value)`.
    Tuple,
    /// Keep `None` and normalize any other value with the inner rule.
    Maybe(&'static Coerce),
    /// Normalize with a native function.
    With(for<'py> fn(&Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>>),
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

/// The rest of `__post_init__`, run on the bound and normalized field values: it may reject them
/// or replace them in place.
pub(crate) type PostInit = for<'py> fn(&mut [Bound<'py, PyAny>]) -> PyResult<()>;

/// One shape: its class name, fields in declaration order and optional post-init hook.
pub(crate) struct ShapeSpec {
    /// The public module path, which with the class name identifies the shape.
    pub(crate) module: &'static str,
    pub(crate) qualname: &'static str,
    pub(crate) fields: &'static [FieldSpec],
    pub(crate) post_init: Option<PostInit>,
    /// Whether the class has the generated repr; false for `@dataclass(repr=False)` shapes.
    pub(crate) repr: bool,
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
/// `@impl { ... }` holding its extra methods and properties. `Name: Base { ... }` subclasses
/// another shape and lists the base fields first, `Name @no_repr { ... }` keeps the object
/// repr, as `@dataclass(repr=False)` did, and `@post_init(hook)` after the fields runs a hook
/// that may reject or replace the bound values.
macro_rules! shapes {
    (@repr) => { true };
    (@repr no_repr) => { false };
    (@post_init) => { None };
    (@post_init $check:path) => { Some($check) };
    (@methods [] $hash:tt $methods:tt impl $name:ident { $($body:tt)* }) => {
        $hash $methods
        impl $name {
            fn __repr__(slf: &pyo3::Bound<'_, Self>) -> pyo3::PyResult<String> {
                $crate::shapes::repr(slf.as_any(), &Self::SPEC, &slf.get().values(slf.py()))
            }

            $($body)*
        }
    };
    (@methods [no_repr] $($item:tt)*) => {
        $($item)*
    };
    (@init $values:ident) => {
        pyo3::PyClassInitializer::from(Self::from_values(&$values)?)
    };
    (@init $values:ident $base:ident) => {
        pyo3::PyClassInitializer::from($base::from_values(&$values)?)
            .add_subclass(Self::from_values(&$values)?)
    };
    (module = $module:literal; $(
        $(#[doc = $doc:literal])*
        $name:ident $(: $base:ident)? $(@$flag:ident)? {
            $($field:ident $(($($key:ident = $value:expr),* $(,)?))?),* $(,)?
        }
        $(@post_init($check:path))?
        $(@impl { $($methods:tt)* })?
    )*) => { $(
        $(#[doc = $doc])*
        #[pyclass(module = $module, frozen, get_all, subclass, weakref $(, extends = $base)?)]
        pub(crate) struct $name {
            $(pub(crate) $field: pyo3::Py<pyo3::PyAny>,)*
        }

        impl $name {
            pub(crate) const SPEC: $crate::shapes::ShapeSpec = $crate::shapes::ShapeSpec {
                module: $module,
                qualname: stringify!($name),
                fields: &[$($crate::shapes::FieldSpec {
                    name: $crate::shapes::unraw(stringify!($field)),
                    $($($key: $value,)*)?
                    ..$crate::shapes::FieldSpec::BASE
                }),*],
                post_init: $crate::shapes::shapes!(@post_init $($check)?),
                repr: $crate::shapes::shapes!(@repr $($flag)?),
            };

            fn values<'py>(&self, py: pyo3::Python<'py>) -> Vec<pyo3::Bound<'py, pyo3::PyAny>> {
                vec![$(self.$field.bind(py).clone()),*]
            }

            /// The shape over the leading bound values, in field order.
            fn from_values(values: &[pyo3::Bound<'_, pyo3::PyAny>]) -> pyo3::PyResult<Self> {
                let mut values = values.iter().cloned();
                Ok(Self { $($field: $crate::shapes::next(&mut values)?,)* })
            }
        }

        $crate::shapes::shapes! { @methods [$($flag)?]
        #[pymethods]
        impl $name {
            #[new]
            #[pyo3(signature = (*args, **kwargs))]
            fn __new__(
                args: &pyo3::Bound<'_, pyo3::types::PyTuple>,
                kwargs: Option<&pyo3::Bound<'_, pyo3::types::PyDict>>,
            ) -> pyo3::PyResult<pyo3::PyClassInitializer<Self>> {
                let values: Vec<pyo3::Bound<'_, pyo3::PyAny>> =
                    match $crate::shapes::copied_values(args, kwargs)? {
                        Some(values) => values,
                        None => $crate::shapes::bind(&Self::SPEC, args, kwargs)?,
                    };
                Ok($crate::shapes::shapes!(@init values $($base)?))
            }

            #[classattr]
            fn __match_args__(
                py: pyo3::Python<'_>,
            ) -> pyo3::PyResult<pyo3::Py<pyo3::types::PyTuple>> {
                $crate::shapes::match_args(py, &Self::SPEC)
            }

            #[classattr]
            fn __dataclass_fields__(py: pyo3::Python<'_>) -> pyo3::PyResult<pyo3::Py<pyo3::PyAny>> {
                $crate::shapes::dataclass_fields(py, &Self::SPEC)
            }

            #[classattr]
            fn __dataclass_params__(py: pyo3::Python<'_>) -> pyo3::PyResult<pyo3::Py<pyo3::PyAny>> {
                $crate::shapes::dataclass_params(py, &Self::SPEC)
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
                $crate::shapes::missing_attribute(slf.as_any(), name)
            }

            /// A new instance of the same class sharing every field value, as `copy.copy` of
            /// the dataclass gave.
            fn __copy__<'py>(
                slf: &pyo3::Bound<'py, Self>,
            ) -> pyo3::PyResult<pyo3::Bound<'py, pyo3::PyAny>> {
                $crate::shapes::shallow_copy(slf.as_any(), slf.get().values(slf.py()))
            }

            /// Rebuilds the shape from its field values, so `deepcopy` and `pickle` work as they
            /// did on the dataclass.
            fn __reduce__<'py>(
                slf: &pyo3::Bound<'py, Self>,
            ) -> pyo3::PyResult<(
                pyo3::Bound<'py, pyo3::types::PyType>,
                pyo3::Bound<'py, pyo3::types::PyTuple>,
            )> {
                let py: pyo3::Python<'py> = slf.py();
                Ok((slf.get_type(), pyo3::types::PyTuple::new(py, slf.get().values(py))?))
            }

            fn __traverse__(&self, visit: pyo3::PyVisit<'_>) -> Result<(), pyo3::PyTraverseError> {
                $(visit.call(&self.$field)?;)*
                Ok(())
            }

            $($($methods)*)?
        }
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

/// The private first constructor argument with which `copy.copy` hands a shape class the field
/// values to keep as they are, so a copy of any subclass is built without rebinding them.
static COPY_MARKER: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

fn copy_marker(py: Python<'_>) -> PyResult<&Py<PyAny>> {
    COPY_MARKER.get_or_try_init(py, || {
        Ok(py.import("builtins")?.getattr("object")?.call0()?.unbind())
    })
}

/// The field values `shallow_copy` passed to the constructor, or `None` for any other call.
pub(crate) fn copied_values<'py>(
    args: &Bound<'py, PyTuple>,
    kwargs: Option<&Bound<'py, PyDict>>,
) -> PyResult<Option<Vec<Bound<'py, PyAny>>>> {
    if args.len() != 2 || kwargs.is_some_and(|kwargs| !kwargs.is_empty()) {
        return Ok(None);
    }
    if !args.get_item(0)?.is(copy_marker(args.py())?) {
        return Ok(None);
    }
    let values: Bound<'py, PyTuple> = args.get_item(1)?.cast_into()?;
    Ok(Some(values.iter().collect()))
}

/// `copy.copy` as it worked on the dataclass: a new instance of the instance's own class that
/// shares every field value, without rerunning normalization, post-init checks or a subclass
/// `__init__`, and with a copy of a Python subclass instance's `__dict__`.
pub(crate) fn shallow_copy<'py>(
    instance: &Bound<'py, PyAny>,
    values: Vec<Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = instance.py();
    let class: Bound<'py, PyType> = instance.get_type();
    let copied: Bound<'py, PyAny> =
        class
            .getattr("__new__")?
            .call1((&class, copy_marker(py)?, PyTuple::new(py, values)?))?;
    if class.getattr("__dictoffset__")?.extract::<isize>()? != 0 {
        copied
            .getattr("__dict__")?
            .call_method1("update", (instance.getattr("__dict__")?,))?;
    }
    Ok(copied)
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
    let defaults: Option<Bound<'py, PyTuple>> = if slots.iter().any(Option::is_none) {
        Some(shared_defaults(py, spec)?)
    } else {
        None
    };
    let mut values: Vec<Bound<'py, PyAny>> = Vec::with_capacity(total);
    for (index, (field, slot)) in spec.fields.iter().zip(slots).enumerate() {
        let value: Bound<'py, PyAny> = match (slot, field.default, &defaults) {
            (Some(value), _, _) => value,
            (None, Given::Factory(factory), _) => factory(py)?.call0()?,
            (None, _, Some(defaults)) => defaults.get_item(index)?,
            (None, _, None) => return Err(PyValueError::new_err("required field has no value")),
        };
        values.push(value);
    }
    for (field, value) in spec.fields.iter().zip(values.iter_mut()) {
        *value = coerce(py, field.coerce, value)?;
    }
    if let Some(check) = spec.post_init {
        check(&mut values)?;
    }
    Ok(values)
}

/// Each shape's plain default values by module path and class name, built once so every
/// instance and the `dataclasses` field table share one default object, as a dataclass's
/// defaults are shared.
static DEFAULTS: PyOnceLock<Py<PyDict>> = PyOnceLock::new();

/// The shape's default values in field order, `None` where a field is required or has a
/// default factory.
fn shared_defaults<'py>(py: Python<'py>, spec: &ShapeSpec) -> PyResult<Bound<'py, PyTuple>> {
    let table: &Bound<'py, PyDict> = DEFAULTS
        .get_or_init(py, || PyDict::new(py).unbind())
        .bind(py);
    let key: (&str, &str) = (spec.module, spec.qualname);
    if let Some(found) = table.get_item(key)? {
        return Ok(found.cast_into()?);
    }
    let mut values: Vec<Bound<'py, PyAny>> = Vec::with_capacity(spec.fields.len());
    for field in spec.fields {
        values.push(match field.default {
            Given::Factory(_) => py.None().into_bound(py),
            given => default_value(py, given)?.unwrap_or_else(|| py.None().into_bound(py)),
        });
    }
    let defaults: Bound<'py, PyTuple> = PyTuple::new(py, values)?;
    table.set_item(key, &defaults)?;
    Ok(defaults)
}

fn default_value(py: Python<'_>, given: Given) -> PyResult<Option<Bound<'_, PyAny>>> {
    Ok(Some(match given {
        Given::Required => return Ok(None),
        Given::None => py.None().into_bound(py),
        Given::Bool(value) => value.into_pyobject(py)?.to_owned().into_any(),
        Given::Int(value) => value.into_pyobject(py)?.into_any(),
        Given::Float(value) => PyFloat::new(py, value).into_any(),
        Given::Str(value) => PyString::new(py, value).into_any(),
        Given::EmptyTuple => PyTuple::empty(py).into_any(),
        Given::EmptyFrozenSet => PyFrozenSet::empty(py)?.into_any(),
        Given::Member(class, member) => native_enum(py, class)?.getattr(member)?,
        Given::Factory(factory) => factory(py)?.call0()?,
        Given::Lazy(build) => build(py)?,
    }))
}

/// `value.first.second...`, read one attribute at a time, as a property's attribute chain.
pub(crate) fn path(py: Python<'_>, value: &Py<PyAny>, names: &[&str]) -> PyResult<Py<PyAny>> {
    let mut current: Bound<'_, PyAny> = value.bind(py).clone();
    for name in names {
        current = current.getattr(*name)?;
    }
    Ok(current.unbind())
}

/// The class a `default_factory` field calls, such as `dict` or a native shape.
pub(crate) fn factory<T: pyo3::PyTypeInfo>(py: Python<'_>) -> PyResult<Bound<'_, PyAny>> {
    Ok(py.get_type::<T>().into_any())
}

fn coerce<'py>(
    py: Python<'py>,
    coerce: Coerce,
    value: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    match coerce {
        Coerce::Keep => Ok(value.clone()),
        Coerce::Enum(class) => native_enum(py, class)?.call1((value,)),
        Coerce::ReadOnlyMapping => py
            .import("types")?
            .getattr("MappingProxyType")?
            .call1((py.get_type::<PyDict>().call1((value,))?,)),
        Coerce::Tuple => py.get_type::<PyTuple>().call1((value,)),
        Coerce::Maybe(inner) if !value.is_none() => self::coerce(py, *inner, value),
        Coerce::Maybe(_) => Ok(value.clone()),
        Coerce::With(normalize) => normalize(value),
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

/// The `__dataclass_fields__` table that `dataclasses.fields`, `replace` and `asdict` read. A
/// shape with a lazy default gets a descriptor that builds the table on first read instead, so
/// the native module never imports the Python class behind that default while it loads.
pub(crate) fn dataclass_fields(py: Python<'_>, spec: &'static ShapeSpec) -> PyResult<Py<PyAny>> {
    if spec
        .fields
        .iter()
        .any(|field| matches!(field.default, Given::Lazy(_)))
    {
        let lazy: LazyDataclassFields = LazyDataclassFields {
            spec,
            table: PyOnceLock::new(),
        };
        return Ok(Bound::new(py, lazy)?.into_any().unbind());
    }
    Ok(field_table(py, spec)?.into_any())
}

/// `__dataclass_fields__` of a shape with a lazy default, built when first read.
#[pyclass(frozen)]
pub(crate) struct LazyDataclassFields {
    spec: &'static ShapeSpec,
    table: PyOnceLock<Py<PyDict>>,
}

#[pymethods]
impl LazyDataclassFields {
    fn __get__(
        &self,
        py: Python<'_>,
        _instance: Option<&Bound<'_, PyAny>>,
        _owner: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<Py<PyDict>> {
        Ok(self
            .table
            .get_or_try_init(py, || field_table(py, self.spec))?
            .clone_ref(py))
    }
}

fn field_table(py: Python<'_>, spec: &ShapeSpec) -> PyResult<Py<PyDict>> {
    let template: &Bound<'_, PyAny> = probe(py)?.0.bind(py);
    let copy: Bound<'_, PyAny> = py.import("copy")?.getattr("copy")?;
    let defaults: Bound<'_, PyTuple> = shared_defaults(py, spec)?;
    let table: Bound<'_, PyDict> = PyDict::new(py);
    for (index, field) in spec.fields.iter().enumerate() {
        let entry: Bound<'_, PyAny> = copy.call1((template,))?;
        entry.setattr("name", field.name)?;
        match field.default {
            Given::Required => {}
            Given::Factory(factory) => entry.setattr("default_factory", factory(py)?)?,
            _ => entry.setattr("default", defaults.get_item(index)?)?,
        }
        entry.setattr("compare", field.compare)?;
        entry.setattr("repr", field.repr)?;
        table.set_item(field.name, entry)?;
    }
    Ok(table.unbind())
}

/// `__dataclass_params__` of a frozen dataclass declared with `repr=False`.
static NO_REPR_PARAMS: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

/// The frozen dataclass parameters of the shape, with `repr` as the dataclass declared it.
pub(crate) fn dataclass_params(py: Python<'_>, spec: &ShapeSpec) -> PyResult<Py<PyAny>> {
    if spec.repr {
        return Ok(probe(py)?.1.clone_ref(py));
    }
    let params: &Py<PyAny> = NO_REPR_PARAMS.get_or_try_init(py, || {
        let keywords: Bound<'_, PyDict> = [("frozen", true), ("repr", false)].into_py_dict(py)?;
        let probe: Bound<'_, PyAny> = py
            .import("dataclasses")?
            .getattr("make_dataclass")?
            .call(("Probe", [("value", "object")]), Some(&keywords))?;
        Ok::<_, PyErr>(probe.getattr("__dataclass_params__")?.unbind())
    })?;
    Ok(params.clone_ref(py))
}

/// `Name(field=value, ...)` over the fields shown in repr, named by the instance's own class
/// as the dataclass repr reads, so a subclass shows its name.
pub(crate) fn repr(
    instance: &Bound<'_, PyAny>,
    spec: &ShapeSpec,
    values: &[Bound<'_, PyAny>],
) -> PyResult<String> {
    let mut parts: Vec<String> = Vec::with_capacity(values.len());
    for (field, value) in spec.fields.iter().zip(values) {
        if field.repr {
            parts.push(format!("{}={}", field.name, value.repr()?.to_str()?));
        }
    }
    let qualname: Bound<'_, PyString> = instance.get_type().qualname()?;
    Ok(format!("{qualname}({})", parts.join(", ")))
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

/// The attribute lookup that failed with `AttributeError`, as the dataclass reported it: a class
/// attribute found on the MRO, such as a property, is read again so its own error surfaces, and
/// any other name gets the missing-attribute error naming the instance's class by its bare name.
pub(crate) fn missing_attribute(
    instance: &Bound<'_, PyAny>,
    name: &Bound<'_, PyString>,
) -> PyResult<Py<PyAny>> {
    let class: Bound<'_, PyType> = instance.get_type();
    for owner in class.getattr("__mro__")?.try_iter()? {
        let namespace: Bound<'_, PyAny> = owner?.getattr("__dict__")?;
        if namespace.contains(name)? {
            let attribute: Bound<'_, PyAny> = namespace.get_item(name)?;
            if let Ok(get) = attribute.get_type().getattr("__get__") {
                return Ok(get.call1((attribute, instance, class))?.unbind());
            }
            break;
        }
    }
    Err(no_attribute(instance, &class.name()?.to_string(), name))
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
    add(module, attribute, class)
}

/// Builds the exception class `qualname` under the public module `public_module`, with `bases`
/// and, when given, a native `__init__` and `__str__` that receive the instance first, and
/// registers it in `sqltide._native` under the same name.
pub(crate) fn exception<'py>(
    module: &Bound<'py, PyModule>,
    public_module: &str,
    qualname: &str,
    doc: &str,
    bases: &[Bound<'py, PyAny>],
    init: Option<Bound<'py, PyAny>>,
    str: Option<Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = module.py();
    let namespace: Bound<'py, PyDict> = PyDict::new(py);
    namespace.set_item("__module__", public_module)?;
    namespace.set_item("__qualname__", qualname)?;
    namespace.set_item("__doc__", doc)?;
    let partialmethod: Bound<'py, PyAny> = py.import("functools")?.getattr("partialmethod")?;
    for (slot, method) in [("__init__", init), ("__str__", str)] {
        if let Some(method) = method {
            namespace.set_item(slot, partialmethod.call1((method,))?)?;
        }
    }
    let class: Bound<'py, PyAny> =
        py.get_type::<PyType>()
            .call1((qualname, PyTuple::new(py, bases)?, namespace))?;
    add(module, qualname, &class)?;
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

/// Adds `value` to `sqltide._native` as `name`, refusing a name the module already defines so
/// a shape, enum, exception or constant never silently replaces another native name.
pub(crate) fn add<'py>(
    module: &Bound<'py, PyModule>,
    name: &str,
    value: impl IntoPyObject<'py>,
) -> PyResult<()> {
    if module.hasattr(name)? {
        return Err(PyImportError::new_err(format!(
            "sqltide._native already defines {name}; register the shape, enum, exception or \
             constant under a distinct name"
        )));
    }
    module.add(name, value)
}

/// Adds the native shape class `T` to `sqltide._native` under its class name, refusing a name
/// the module already defines.
pub(crate) fn add_class<T: pyo3::PyTypeInfo>(module: &Bound<'_, PyModule>) -> PyResult<()> {
    let class: Bound<'_, PyType> = module.py().get_type::<T>();
    let name: Bound<'_, PyString> = class.name()?;
    add(module, name.to_str()?, class)
}

/// The native exception `name` raised with `message`.
pub(crate) fn raised(py: Python<'_>, name: &str, message: String) -> PyResult<PyErr> {
    Ok(PyErr::from_value(
        py.import("sqltide._native")?
            .getattr(name)?
            .call1((message,))?,
    ))
}

/// Constants built on first read, by name.
static LAZY_VALUES: PyOnceLock<Py<PyDict>> = PyOnceLock::new();

/// Serves `constants` from `sqltide._native.__getattr__`, each built on first read and kept, for
/// values made from a class still defined in Python so the native module never imports it while
/// it loads. Every other missing name raises the `AttributeError` a module raises.
pub(crate) fn lazy_constants(
    module: &Bound<'_, PyModule>,
    constants: &'static [(&'static str, Factory)],
) -> PyResult<()> {
    for (name, _) in constants {
        if module.hasattr(*name)? {
            return Err(PyImportError::new_err(format!(
                "sqltide._native already defines {name}; register the constant under a \
                 distinct name"
            )));
        }
    }
    let owner: Py<PyModule> = module.clone().unbind();
    let getattr = PyCFunction::new_closure(
        module.py(),
        Some(c"__getattr__"),
        None,
        move |args: &Bound<'_, PyTuple>,
              _kwargs: Option<&Bound<'_, PyDict>>|
              -> PyResult<Py<PyAny>> {
            let py: Python<'_> = args.py();
            let name: Bound<'_, PyString> = args.get_item(0)?.cast_into()?;
            let Some((_, build)) = constants
                .iter()
                .find(|(key, _)| name.to_str().is_ok_and(|text| text == *key))
            else {
                let module: &Bound<'_, PyModule> = owner.bind(py);
                let message: String =
                    format!("module '{}' has no attribute '{name}'", module.name()?);
                let keywords: Bound<'_, PyDict> =
                    [("name", name.as_any()), ("obj", module.as_any())].into_py_dict(py)?;
                return Err(PyErr::from_value(
                    py.get_type::<PyAttributeError>()
                        .call((message,), Some(&keywords))?,
                ));
            };
            let cache: &Bound<'_, PyDict> = LAZY_VALUES
                .get_or_init(py, || PyDict::new(py).unbind())
                .bind(py);
            if let Some(value) = cache.get_item(&name)? {
                return Ok(value.unbind());
            }
            let value: Bound<'_, PyAny> = build(py)?;
            Ok(cache.call_method1("setdefault", (&name, value))?.unbind())
        },
    )?;
    if module.hasattr("__getattr__")? {
        return Err(PyImportError::new_err(
            "sqltide._native already defines __getattr__",
        ));
    }
    module.setattr("__getattr__", getattr)
}

/// Registers every native shape, enum, exception and constant. It runs after every other
/// registration so the name guard sees the whole module.
pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    compiler_analysis::register(module)?;
    compiler_project::register(module)?;
    planner_execution::register(module)?;
    executor_ops::register(module)?;
    cli_service::register(module)?;
    sensors_dev::register(module)?;
    discovery_models::register(module)?;
    adapter_models::register(module)?;
    lazy_constants(
        module,
        &[
            (
                "CLICKHOUSE_REPLAY_BOUNDARY_MODES",
                planner_execution::clickhouse_replay_boundary_modes,
            ),
            ("SQLTIDE_TOOL_VERSION", cli_service::tool_version),
            ("WORKFLOW_PHASE_ORDER", cli_service::workflow_phase_order),
        ],
    )
}
