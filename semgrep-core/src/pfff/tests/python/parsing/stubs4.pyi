@overload
def default_if_none(factory: Callable[[], _T]) -> _ConverterType[_T]: ...