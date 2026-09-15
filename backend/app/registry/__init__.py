"""注册制机制层。

- `schema_registry`：把「配置表」注册进系统，注册后自动获得
  列表 / 新增 / 修改（乐观锁）/ 删除 / 审计 / 回滚 / 管理 API。
- `adapters`：把「模型协议」注册进系统，加一种协议 = 加一个文件 + 注册一行。

核心框架只依赖这两个注册表，不依赖任何具体表名或厂商名。
"""

from app.registry import adapters, schema_registry

__all__ = ["adapters", "schema_registry"]
