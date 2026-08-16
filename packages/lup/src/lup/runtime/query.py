"""The small typed one-turn convenience operation.

`query(factory, ...)` is deliberately the same function object as
`SessionFactory.query`, not a wrapper around it: the free spelling therefore
shares the method's overload set and its exact inference instead of copying
three overloads and the normalisation they sit on. The price is that the free
spelling's first parameter is named `self`, which is why it stays named that.
"""

from lup.runtime.factory import SessionFactory

query = SessionFactory.query
