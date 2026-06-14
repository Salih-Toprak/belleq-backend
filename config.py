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

    # Conversation fact-extraction credentials. Set these ONCE here on the
    # (static) backend; they are pushed to every context at provision time, so
    # the ephemeral masters/containers never store them. Set GEMINI_API_KEY and
    # extraction turns on automatically.
    CONVERSATION_EXTRACTION_ENABLED: bool = True
    EXTRACTION_BACKEND: str = "gemini"  # "gemini" | "anthropic"
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-2.5-flash"
    # Only needed if EXTRACTION_BACKEND=anthropic.
    EXTRACTION_ANTHROPIC_API_KEY: str = ""
    EXTRACTION_MODEL: str = "claude-haiku-4-5"

    # Connector durability. Connectors are stored per-account here in Postgres
    # (the only static server) so they survive their EC2 host being terminated.
    # CREDENTIAL_ENCRYPTION_KEY is a Fernet key shared with every master: masters
    # encrypt connector secrets with it before mirroring, and the backend stores
    # only that ciphertext (never decrypts). BACKEND_INTERNAL_TOKEN authenticates
    # masters mirroring back; BACKEND_PUBLIC_URL is the master→backend base URL.
    CREDENTIAL_ENCRYPTION_KEY: str = ""
    BACKEND_INTERNAL_TOKEN: str = ""
    BACKEND_PUBLIC_URL: str = ""  # e.g. https://api.belleq.app

    INTERNAL_POLL_INTERVAL: int = 15
    INTERNAL_POLL_TIMEOUT: int = 600

    # Background sweep that terminates hosts with no active contexts so we never
    # pay for idle EC2. Belt-and-suspenders alongside the per-delete teardown.
    EMPTY_HOST_SWEEP_ENABLED: bool = True
    EMPTY_HOST_SWEEP_INTERVAL: int = 600          # seconds between sweeps
    EMPTY_HOST_SWEEP_MIN_AGE_MINUTES: int = 15    # skip hosts younger than this
    CORS_ORIGINS: str = "http://localhost:3000,https://belleq.app,https://www.belleq.app"

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",")]

    @property
    def extraction_payload(self) -> dict:
        """Extraction config sent to the master at provision time.

        ``enabled`` is true only when extraction is on AND a key exists for the
        chosen backend — so setting just the Gemini key is enough to turn it on,
        and leaving it blank produces no noisy extraction failures.
        """
        backend = (self.EXTRACTION_BACKEND or "gemini").strip().lower()
        key = self.GEMINI_API_KEY if backend == "gemini" else self.EXTRACTION_ANTHROPIC_API_KEY
        return {
            "enabled": bool(self.CONVERSATION_EXTRACTION_ENABLED and (key or "").strip()),
            "backend": backend,
            "gemini_api_key": self.GEMINI_API_KEY,
            "gemini_model": self.GEMINI_MODEL,
            "anthropic_api_key": self.EXTRACTION_ANTHROPIC_API_KEY,
            "extraction_model": self.EXTRACTION_MODEL,
        }

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
