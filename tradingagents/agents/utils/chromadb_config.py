"""
ChromaDB 统一配置模块
支持 Windows 10/11 和其他操作系统的自动适配

RAG 升级 (2026-08-02): 新增持久化配置，支持外部知识库
  - is_persistent 由环境变量 CHROMADB_IS_PERSISTENT 控制（默认 true）
  - persist_directory 由环境变量 CHROMADB_PERSIST_DIR 控制（默认 ./data/chromadb）
  - 清理 chromadb>=1.0 已废弃的 chroma_db_impl / chroma_api_impl 参数
"""
import os
import platform
import chromadb
from chromadb.config import Settings

# ============================================================
#  持久化配置 (RAG 升级)
# ============================================================
CHROMADB_PERSIST_DIR = os.getenv("CHROMADB_PERSIST_DIR", "./data/chromadb")
CHROMADB_IS_PERSISTENT = os.getenv("CHROMADB_IS_PERSISTENT", "true").lower() == "true"


def is_windows_11() -> bool:
    """
    检测是否为 Windows 11

    Returns:
        bool: 如果是 Windows 11 返回 True，否则返回 False
    """
    if platform.system() != "Windows":
        return False

    # Windows 11 的版本号通常是 10.0.22000 或更高
    version = platform.version()
    try:
        version_parts = version.split('.')
        if len(version_parts) >= 3:
            build_number = int(version_parts[2])
            return build_number >= 22000
    except (ValueError, IndexError):
        pass

    return False


def _build_settings(include_persist: bool = True) -> Settings:
    """
    构建 ChromaDB Settings，统一持久化配置

    chromadb>=1.0 已废弃 chroma_db_impl / chroma_api_impl 参数，不再使用。
    """
    settings_kwargs = dict(
        allow_reset=True,
        anonymized_telemetry=False,
        is_persistent=CHROMADB_IS_PERSISTENT if include_persist else False,
    )
    if CHROMADB_IS_PERSISTENT and include_persist:
        # 确保持久化目录存在
        os.makedirs(CHROMADB_PERSIST_DIR, exist_ok=True)
        settings_kwargs["persist_directory"] = CHROMADB_PERSIST_DIR
    return Settings(**settings_kwargs)


def get_win10_chromadb_client():
    """
    获取 Windows 10 兼容的 ChromaDB 客户端

    Returns:
        chromadb.Client: ChromaDB 客户端实例
    """
    try:
        return chromadb.Client(_build_settings())
    except Exception:
        # 降级到最基本配置
        return chromadb.Client(Settings(
            allow_reset=True,
            is_persistent=CHROMADB_IS_PERSISTENT,
        ))


def get_win11_chromadb_client():
    """
    获取 Windows 11 优化的 ChromaDB 客户端

    Returns:
        chromadb.Client: ChromaDB 客户端实例
    """
    try:
        return chromadb.Client(_build_settings())
    except Exception:
        # 最简配置降级
        return chromadb.Client(Settings(
            allow_reset=True,
            anonymized_telemetry=False,
            is_persistent=CHROMADB_IS_PERSISTENT,
        ))


def get_optimal_chromadb_client():
    """
    根据操作系统自动选择最优 ChromaDB 配置

    Returns:
        chromadb.Client: ChromaDB 客户端实例
    """
    system = platform.system()

    if system == "Windows":
        if is_windows_11():
            return get_win11_chromadb_client()
        else:
            return get_win10_chromadb_client()
    else:
        # 非 Windows 系统，使用标准配置
        return chromadb.Client(_build_settings())


# 导出配置
__all__ = [
    'get_optimal_chromadb_client',
    'get_win10_chromadb_client',
    'get_win11_chromadb_client',
    'is_windows_11',
    'CHROMADB_PERSIST_DIR',
    'CHROMADB_IS_PERSISTENT',
]
