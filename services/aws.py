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

# ── System update + install deps ─────────────────────────────────────────────
dnf update -y
dnf install -y docker git curl ec2-instance-connect

# ── Docker ───────────────────────────────────────────────────────────────────
systemctl enable docker
systemctl start docker
usermod -aG docker ec2-user

# Wait until Docker daemon is ready
timeout 60 bash -c 'until docker info &>/dev/null; do sleep 2; done'

# ── Docker Compose v2 ────────────────────────────────────────────────────────
COMPOSE_VERSION=$(curl -fsSL https://api.github.com/repos/docker/compose/releases/latest \
  | grep '"tag_name"' | head -1 | cut -d'"' -f4)
mkdir -p /usr/local/lib/docker/cli-plugins
curl -fsSL "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-x86_64" \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
docker compose version

# ── Clone master repo ────────────────────────────────────────────────────────
cd /home/ec2-user
git clone https://github.com/sstprk/mnemo_master.git belleq
chown -R ec2-user:ec2-user belleq
cd belleq

cat > .env << 'ENVEOF'
ADMIN_API_KEY={master_api_key}
QDRANT_URL=http://belleq-qdrant:6333
QDRANT_COLLECTION=belleq_knowledge
ENVEOF

# ── Start the stack ──────────────────────────────────────────────────────────
docker network create belleq-net || true
docker compose up -d

echo "Bootstrap complete"
"""


def _get_ec2_client(region: str):
    return boto3.client(
        "ec2",
        region_name=region,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
    )


def _launch_instance(instance_name: str, master_api_key: str, region: str) -> str:
    ec2 = _get_ec2_client(region)
    user_data = BOOTSTRAP_SCRIPT.replace("{master_api_key}", master_api_key)

    params = {
        "ImageId": settings.AWS_AMI_ID,
        "InstanceType": settings.AWS_INSTANCE_TYPE,
        "MinCount": 1,
        "MaxCount": 1,
        "UserData": base64.b64encode(user_data.encode()).decode(),
        "TagSpecifications": [
            {
                "ResourceType": "instance",
                "Tags": [{"Key": "Name", "Value": instance_name}],
            }
        ],
    }

    if settings.AWS_SECURITY_GROUP_ID:
        params["SecurityGroupIds"] = [settings.AWS_SECURITY_GROUP_ID]

    if settings.AWS_KEY_PAIR_NAME:
        params["KeyName"] = settings.AWS_KEY_PAIR_NAME

    logger.info("Launching EC2 instance: name=%s region=%s type=%s", instance_name, region, settings.AWS_INSTANCE_TYPE)
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


async def provision_ec2(instance_name: str, master_api_key: str, region: str) -> dict:
    instance_id = await asyncio.to_thread(_launch_instance, instance_name, master_api_key, region)
    public_ip = await asyncio.to_thread(_wait_for_public_ip, instance_id, region)
    return {"instance_id": instance_id, "public_ip": public_ip}


async def terminate_ec2(instance_id: str, region: str):
    await asyncio.to_thread(_terminate_instance, instance_id, region)
