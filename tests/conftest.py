"""端到端测试夹具：内存库 + Flask 测试客户端。"""

import pytest
from sqlalchemy import create_engine

from pharmacy_identity import create_app


@pytest.fixture()
def client():
    engine = create_engine("sqlite:///:memory:")
    app = create_app(engine)
    with app.test_client() as test_client:
        yield test_client
