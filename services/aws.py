import asyncio
import base64
import logging
import time

import boto3

from config import settings

logger = logging.getLogger(__name__)

BOOTSTRAP_SCRIPT = """#!/bin/bash
exec > /var/log/belleq-bootstrap.log 2>&1
set -ex

# ── Base deps ────────────────────────────────────────────────────────────────
dnf install -y git ec2-instance-connect

# ── Docker (AL2023 native packages) ─────────────────────────────────────────
dnf install -y docker
systemctl enable docker
systemctl start docker
usermod -aG docker ec2-user

# ── Docker Compose v2 + Buildx plugins ───────────────────────────────────────
mkdir -p /usr/local/lib/docker/cli-plugins
curl -SL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64" \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

BUILDX_VERSION=$(curl -s https://api.github.com/repos/docker/buildx/releases/latest | grep '"tag_name"' | cut -d'"' -f4)
curl -SL "https://github.com/docker/buildx/releases/download/${BUILDX_VERSION}/buildx-${BUILDX_VERSION}.linux-amd64" \
  -o /usr/local/lib/docker/cli-plugins/docker-buildx
chmod +x /usr/local/lib/docker/cli-plugins/docker-buildx

# Wait until Docker daemon is ready
timeout 120 bash -c 'until docker info &>/dev/null; do sleep 2; done'
docker version
docker compose version

# ── Clone master repo ────────────────────────────────────────────────────────
cd /home/ec2-user
REPO_URL="https://github.com/{master_repo}"
if [ -n "{github_token}" ]; then
  REPO_URL="https://x-access-token:{github_token}@github.com/{master_repo}"
fi
git clone "$REPO_URL" belleq
chown -R ec2-user:ec2-user belleq
cd belleq

cat > .env << 'ENVEOF'
ADMIN_API_KEY={master_api_key}
QDRANT_URL=http://belleq-qdrant:6333
QDRANT_COLLECTION=belleq_knowledge
EMBEDDING_BACKEND=ollama
OLLAMA_BASE_URL={embedding_url}
OLLAMA_EMBED_MODEL={embedding_model}
ENVEOF

# ── Authenticate to GHCR ──────────────────────────────────────────────────────
# Needed only if the master/user packages are private. Harmless if public.
# The token must carry read:packages scope to pull private images.
if [ -n "{github_token}" ]; then
  echo "{github_token}" | docker login ghcr.io -u x-access-token --password-stdin || true
fi

# ── Start the stack ──────────────────────────────────────────────────────────
# Pull the prebuilt master image from GHCR instead of building from source on
# every boot (faster, and avoids re-resolving Python deps per environment).
# The repo is still cloned for the compose file + config; --no-build skips the
# local image build now that an image: is set in docker-compose.yml.
docker compose pull
docker compose up -d --no-build

echo "Bootstrap complete"
"""


def _get_ec2_client(region: str):
    return boto3.client(
        "ec2",
        region_name=region,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
    )


def _launch_instance(
    instance_name: str,
    master_api_key: str,
    region: str,
    instance_type: str | None = None,
    tags: list[dict] | None = None,
) -> str:
    ec2 = _get_ec2_client(region)
    user_data = BOOTSTRAP_SCRIPT.replace("{master_api_key}", master_api_key)
    user_data = user_data.replace("{master_repo}", settings.BELLEQ_MASTER_IMAGE)
    user_data = user_data.replace("{github_token}", settings.GITHUB_TOKEN)
    user_data = user_data.replace("{embedding_url}", settings.EMBEDDING_OLLAMA_URL)
    user_data = user_data.replace("{embedding_model}", settings.EMBEDDING_MODEL)

    itype = instance_type or settings.AWS_INSTANCE_TYPE or "t3.large"
    instance_tags = tags or [{"Key": "Name", "Value": instance_name}]

    params = {
        "ImageId": settings.AWS_AMI_ID,
        "InstanceType": itype,
        "MinCount": 1,
        "MaxCount": 1,
        "UserData": base64.b64encode(user_data.encode()).decode(),
        "TagSpecifications": [
            {"ResourceType": "instance", "Tags": instance_tags},
        ],
    }

    if settings.AWS_SECURITY_GROUP_ID:
        params["SecurityGroupIds"] = [settings.AWS_SECURITY_GROUP_ID]

    if settings.AWS_KEY_PAIR_NAME:
        params["KeyName"] = settings.AWS_KEY_PAIR_NAME

    logger.info("Launching EC2 instance: name=%s region=%s type=%s", instance_name, region, itype)
    response = ec2.run_instances(**params)
    instance_id = response["Instances"][0]["InstanceId"]
    logger.info("EC2 instance launched: %s", instance_id)
    return instance_id


def _wait_for_public_ip(instance_id: str, region: str, timeout: int = 120) -> str:
    ec2 = _get_ec2_client(region)
    start = time.time()
    while time.time() - start < timeout:
        resp = ec2.describe_instances(InstanceIds=[instance_id])
        instance = resp["Reservations"][0]["Instances"][0]
        public_ip = instance.get("PublicIpAddress")
        if public_ip:
            logger.info("EC2 %s got public IP: %s", instance_id, public_ip)
            return public_ip
        time.sleep(5)
    raise TimeoutError(f"EC2 {instance_id} did not get a public IP within {timeout}s")


def _terminate_instance(instance_id: str, region: str):
    ec2 = _get_ec2_client(region)
    logger.info("Terminating EC2 instance: %s", instance_id)
    ec2.terminate_instances(InstanceIds=[instance_id])


async def provision_ec2(
    instance_name: str,
    master_api_key: str,
    region: str,
    instance_type: str | None = None,
    tags: list[dict] | None = None,
) -> dict:
    instance_id = await asyncio.to_thread(
        _launch_instance, instance_name, master_api_key, region, instance_type, tags
    )
    public_ip = await asyncio.to_thread(_wait_for_public_ip, instance_id, region)
    return {"instance_id": instance_id, "public_ip": public_ip}


async def terminate_ec2(instance_id: str, region: str):
    await asyncio.to_thread(_terminate_instance, instance_id, region)
