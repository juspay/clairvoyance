import os
from typing import Dict, Any, Optional
from .providers.dev_cycle import DevCycleProvider


class FeatureFlagManager:
    def __init__(self):
        self.provider = None
        self._initialize_provider()
    
    def _initialize_provider(self):
        # Simple: only check for DevCycle key
        if os.environ.get("DEVCYCLE_SERVER_SDK_KEY"):
            self.provider = DevCycleProvider()
        else:
            self.provider = None
    
    def is_enabled(self, env_key: str, default_value: bool = False, user_data: Optional[Dict[str, Any]] = None) -> bool:
        if self.provider:
            return self.provider.is_enabled(env_key, default_value, user_data)
        # Fallback to environment variables
        return os.environ.get(env_key, str(default_value)).lower() == "true"
    
    def get_string(self, env_key: str, default_value: str = "", user_data: Optional[Dict[str, Any]] = None) -> str:
        if self.provider:
            return self.provider.get_string(env_key, default_value, user_data)
        return os.environ.get(env_key, default_value)
    
    def get_number(self, env_key: str, default_value: float = 0.0, user_data: Optional[Dict[str, Any]] = None) -> float:
        if self.provider:
            return self.provider.get_number(env_key, default_value, user_data)
        try:
            return float(os.environ.get(env_key, str(default_value)))
        except ValueError:
            return default_value
    
    def get_variable(self, env_key: str, default_value: Any = None, user_data: Optional[Dict[str, Any]] = None) -> Any:
        if self.provider:
            return self.provider.get_variable(env_key, default_value, user_data)
        return os.environ.get(env_key, default_value)


# Global instance
feature_flags = FeatureFlagManager()
