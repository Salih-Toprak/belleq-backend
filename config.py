from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    SUPABASE_URL: str = ""
    SUPABASE_SERVICE_KEY: str = ""

    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    AWS_REGION: str = "eu-west-1"
    AWS_AMI_ID: str = "ami-0c02fb55956c7d316"
    # Overrides the per-plan EC2 type for every host the scheduler launches —
    # e.g. set to "m7i-flex.large" to stay inside the new-account free tier.
    # Empty = use each plan's instance type from plan_config.py.
    AWS_INSTANCE_TYPE: str = ""
    AWS_KEY_PAIR_NAME: str = ""
    AWS_SECURITY_GROUP_ID: str = ""

    GITHUB_TOKEN: str = ""
    BELLEQ_MASTER_IMAGE: str = "Salih-Toprak/belleq-master.git"
    BELLEQ_CONTAINER_IMAGE: str = "Salih-Toprak/belleq-user.git"

    # Central embedding service (one shared Ollama for every host). Masters and
    # their context containers point here instead of running their own.
    EMBEDDING_OLLAMA_URL: str = ""  # e.g. http://10.0.1.20:11434
    EMBEDDING_MODEL: str = "nomic-embed-text"

    INTERNAL_POLL_INTERVAL: int = 15
    INTERNAL_POLL_TIMEOUT: int = 600
    CORS_ORIGINS: str = "http://localhost:3000,https://belleq.app,https://www.belleq.app"

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",")]

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
