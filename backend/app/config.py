"""运行配置。

默认连接本机用户态 PostgreSQL（见 scripts/pg_start.sh），
可用环境变量 RAWMIX_DATABASE_URL 覆盖。
"""
import os

DEFAULT_DATABASE_URL = (
    "postgresql+psycopg2://mixapp@127.0.0.1:55432/rawmix"
)
DATABASE_URL = os.environ.get("RAWMIX_DATABASE_URL", DEFAULT_DATABASE_URL)

# 氧化物口径：归一化/守恒合成时只关心下列氧化物 + 烧失量
OXIDES = ["CaO", "SiO2", "Al2O3", "Fe2O3"]
HAZARDOUS = ["MgO", "SO3", "K2O", "Na2O", "Cl", "alkali_eq", "R2O"]

# 碱当量折算系数（以 Na2O 当量计）：Na2O + 0.658 K2O
ALKALI_EQ_FACTOR_K2O = 0.658
