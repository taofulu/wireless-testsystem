"""运行期配置。所有环境相关取值集中于此（参见工程约定：阈值/配置集中）。"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WTS_", env_file=None)

    # 默认指向本地 PostgreSQL；测试环境通过 WTS_DATABASE_URL 覆盖为 sqlite
    database_url: str = "postgresql+psycopg2://wts:wts@localhost:5432/wts"


settings = Settings()
