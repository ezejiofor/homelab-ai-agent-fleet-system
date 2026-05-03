# SkillsClient — Shared Library

Copy or mount `skills_client.py` into any agent container to enable skill learning.

```python
from skills_client import SkillsClient
skills = SkillsClient()  # reads SKILLSHUB_URL + SKILLSHUB_API_KEY from env
```

All methods are async. See inline docstrings for full usage.
