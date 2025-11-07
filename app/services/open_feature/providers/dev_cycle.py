import os
from typing import Dict, Any, Optional
from openfeature import api
from openfeature.api import get_client
from openfeature.evaluation_context import EvaluationContext
from devcycle_python_sdk import DevCycleLocalClient, DevCycleLocalOptions

from loguru import logger


class DevCycleProvider:
    """
    DevCycle provider for OpenFeature.
    Provides real-time feature flag control with safe fallbacks to environment variables.
    
    Usage:
        - Use is_enabled() for feature flags (on/off switches)
        - Use get_variable() for DevCycle variables (configurable values)
    """
    
    def __init__(self):
        self.sdk_key = os.environ.get("DEVCYCLE_SERVER_SDK_KEY")
        self.client = None
        self.fallback_mode = not bool(self.sdk_key)
        
        self._initialize_openfeature()
    
    def _initialize_openfeature(self):
        """Initialize OpenFeature with DevCycle provider"""
        if not self.sdk_key:
            logger.warning("DEVCYCLE_SERVER_SDK_KEY not found, using fallback mode")
            return
        
        try:
            # Create options for optimal performance
            options = DevCycleLocalOptions(
                config_polling_interval_ms=10000,  # Poll every 10 seconds
                disable_realtime_updates=False,    # Enable realtime updates
            )
            
            # Create DevCycle client instance
            devcycle_client = DevCycleLocalClient(self.sdk_key, options)
            
            # Set the provider for OpenFeature
            api.set_provider(devcycle_client.get_openfeature_provider())
            
            # Get the OpenFeature client
            self.client = api.get_client()
            
            logger.info("OpenFeature initialized with DevCycle provider")
        except Exception as e:
            logger.error(f"Failed to initialize OpenFeature: {e}")
            self.client = None
    
    def _devcycle_to_env_key(self, feature_key: str) -> str:
        """
        Convert DevCycle feature key to environment variable name.
        
        Universal Convention: Add ENABLE_ prefix, uppercase and replace - with _
        
        Examples:
        - voice-noise-reduction → ENABLE_VOICE_NOISE_REDUCTION
        - deepgram-vad-events → ENABLE_DEEPGRAM_VAD_EVENTS
        - aic-enhancement-level → ENABLE_AIC_ENHANCEMENT_LEVEL
        """
        return feature_key.upper().replace('-', '_')
    
    def _get_env_fallback(self, feature_key: str) -> bool:
        """
        Get environment variable fallback value using convention.
        
        Args:
            feature_key: The feature flag key (e.g., "voice-noise-reduction")
            
        Returns:
            True if environment variable is set to "true", False otherwise
        """
        env_var = self._devcycle_to_env_key(feature_key)
        env_value = os.environ.get(env_var, "false").lower()
        return env_value == "true"
    
    def is_enabled(self, env_key: str, default_value: bool = False, user_data: Optional[Dict[str, Any]] = None) -> bool:
        """
        Check if a feature flag is enabled with DevCycle first, then fallback to environment variables.
        
        Use this method for feature flags (on/off switches), not variables.
        
        Args:
            env_key: The environment variable key (e.g., "ENABLE_NOISE_REDUCTION")
            default_value: Default value if all fallbacks fail
            user_data: Optional user context for targeting
            
        Returns:
            True if feature is enabled, False otherwise
        """
        # Convert env_key to DevCycle key for DevCycle lookup
        devcycle_key = env_key.lower().replace('_', '').replace('enable', '')
        
        # Try DevCycle first - check if flag exists and get its value
        if not self.fallback_mode and self.client:
            try:
                context = self._create_evaluation_context(user_data)
                
                result = self.client.get_boolean_value(
                    flag_key=devcycle_key,
                    default_value=None,  
                    evaluation_context=context
                )
                
                if result is not None:  # Flag exists in DevCycle
                    logger.debug(f"DevCycle flag {devcycle_key}: {result}")
                    return result
                else:  # Flag doesn't exist in DevCycle, use fallback
                    logger.debug(f"DevCycle flag {devcycle_key} not found, using environment fallback")
                    
            except Exception as e:
                logger.warning(f"DevCycle failed for {devcycle_key}, using fallback: {e}")
        
        # Fallback to original environment variable (no conversion needed)
        env_value = os.environ.get(env_key, str(default_value)).lower()
        result = env_value == "true"
        logger.debug(f"Environment fallback for {devcycle_key} ({env_key}): {result}")
        return result
    
    def get_variable(self, variable_key: str, default_value: bool = False, user_data: Optional[Dict[str, Any]] = None) -> bool:
        """
        Get DevCycle variable value with comprehensive fallback system.
                                                                                                                                                       
        Use this method for DevCycle variables, not feature flags.
        
        This implements the two-step DevCycle approach:
        1. Try DevCycle variables using the SDK's variable methods
        2. Fallback to environment variables
        3. Use provided default as final fallback
        
        Args:
            variable_key: The DevCycle variable key (e.g., "voice-noise-reduction")
            default_value: Default value if all fallbacks fail
            user_data: Optional user context for targeting
            
        Returns:
            True if variable is enabled, False otherwise
        """
        # Try DevCycle variables using the SDK's variable methods
        if not self.fallback_mode and self.client:
            try:
                context = self._create_evaluation_context(user_data)
                
                # Use the OpenFeature client to get variable values
                result = self.client.get_boolean_value(
                    flag_key=variable_key,
                    default_value=default_value,
                    evaluation_context=context
                )
                
                # Check if we got a valid result (not the default)
                if result != default_value:
                    logger.debug(f"DevCycle variable {variable_key}: {result}")
                    return result
                else:
                    logger.debug(f"DevCycle variable {variable_key} not found or default, using environment fallback")
                    
            except Exception as e:
                logger.warning(f"DevCycle variable {variable_key} failed, using fallback: {e}")
        
        # Fallback to environment variables
        env_value = os.environ.get(variable_key.upper().replace('-', '_'), str(default_value))
        return env_value == 'true' if isinstance(default_value, bool) else env_value

    def get_string(self, env_key: str, default_value: str = "", user_data: Optional[Dict[str, Any]] = None) -> str:
        """Get string feature variable value"""
        # Convert env_key to DevCycle key for DevCycle lookup
        devcycle_key = env_key.lower().replace('_', '-')
        
        if not self.fallback_mode and self.client:
            try:
                context = self._create_evaluation_context(user_data)
                result = self.client.get_string_value(
                    flag_key=devcycle_key,
                    default_value=default_value,
                    evaluation_context=context
                )
                if result != default_value:  # Got valid result from DevCycle
                    logger.debug(f"DevCycle string {devcycle_key}: {result}")
                    return result
                else:  # Use environment fallback
                    logger.debug(f"DevCycle string {devcycle_key} not found, using environment fallback")
            except Exception as e:
                logger.warning(f"DevCycle failed for {devcycle_key}, using fallback: {e}")
        
        # Fallback to original environment variable
        result = os.environ.get(env_key, default_value)
        logger.debug(f"Environment fallback for {devcycle_key} ({env_key}): {result}")
        return result
    
    def get_number(self, env_key: str, default_value: float = 0.0, user_data: Optional[Dict[str, Any]] = None) -> float:
        """Get numeric feature variable value"""
        # Convert env_key to DevCycle key for DevCycle lookup
        devcycle_key = env_key.lower().replace('_', '-')
        
        if not self.fallback_mode and self.client:
            try:
                context = self._create_evaluation_context(user_data)
                result = self.client.get_number_value(
                    flag_key=devcycle_key,
                    default_value=default_value,
                    evaluation_context=context
                )
                if result != default_value:  # Got valid result from DevCycle
                    logger.debug(f"DevCycle number {devcycle_key}: {result}")
                    return result
                else:  # Use environment fallback
                    logger.debug(f"DevCycle number {devcycle_key} not found, using environment fallback")
            except Exception as e:
                logger.warning(f"DevCycle failed for {devcycle_key}, using fallback: {e}")
        
        # Fallback to original environment variable
        try:
            result = float(os.environ.get(env_key, str(default_value)))
            logger.debug(f"Environment fallback for {devcycle_key} ({env_key}): {result}")
            return result
        except (ValueError, TypeError):
            logger.debug(f"Invalid number in environment for {env_key}, using default: {default_value}")
            return default_value
    
    def _create_evaluation_context(self, user_data: Optional[Dict[str, Any]] = None) -> EvaluationContext:
        """
        Create EvaluationContext for DevCycle flag evaluation.
        
        Args:
            user_data: Optional user data for targeting
            
        Returns:
            EvaluationContext with required targeting_key and attributes
        """
        # Use provided user data or create default context
        if user_data and 'user_id' in user_data:
            targeting_key = user_data['user_id']
            attributes = {k: v for k, v in user_data.items() if k != 'user_id'}
        else:
            # Default context for server-side usage
            targeting_key = os.environ.get("DEVCYCLE_DEFAULT_TARGETING_KEY")
            attributes = {
                "type": "server",
                "environment": os.environ.get("ENVIRONMENT", "development"),
            }
        
        return EvaluationContext(
            targeting_key=targeting_key,
            attributes=attributes
        )
