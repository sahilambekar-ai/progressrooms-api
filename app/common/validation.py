from typing import Annotated
from pydantic import StringConstraints

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
Slug = Annotated[str, StringConstraints(min_length=1, max_length=255, pattern=r'^[a-z0-9][a-z0-9_-]*$')]
Currency = Annotated[str, StringConstraints(to_upper=True, pattern=r'^[A-Z]{3}$')]
