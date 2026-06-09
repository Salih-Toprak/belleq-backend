#!/usr/bin/env python3
"""Validate the Belleq AWS setup without launching anything.

Reads the same settings the backend uses (config.py / .env) and checks that the
credentials work and every referenced resource exists in the configured region.

    cd belleq-backend
    python scripts/check_aws.py

Exit code 0 = everything the provisioner needs is in place.
"""
import sys
from pathlib import Path

# Allow running from the repo root or the scripts/ dir.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from config import settings

OK = "\033[32m✓\033[0m"
BAD = "\033[31m✗\033[0m"
WARN = "\033[33m!\033[0m"


def _client(service):
    return boto3.client(
        service,
        region_name=settings.AWS_REGION,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
    )


def main() -> int:
    problems = 0
    print(f"Region: {settings.AWS_REGION or '(unset)'}\n")

    # 1. Credentials / identity
    try:
        ident = _client("sts").get_caller_identity()
        print(f"{OK} Credentials valid — account {ident['Account']}, arn {ident['Arn']}")
    except (ClientError, BotoCoreError) as e:
        print(f"{BAD} Credentials FAILED: {e}")
        print("    → Check AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_REGION in .env")
        return 1  # nothing else will work

    ec2 = _client("ec2")

    # 2. AMI exists in this region
    if not settings.AWS_AMI_ID:
        print(f"{BAD} AWS_AMI_ID is empty")
        problems += 1
    else:
        try:
            imgs = ec2.describe_images(ImageIds=[settings.AWS_AMI_ID])["Images"]
            if imgs:
                print(f"{OK} AMI {settings.AWS_AMI_ID} found — {imgs[0].get('Name','')}")
            else:
                print(f"{BAD} AMI {settings.AWS_AMI_ID} not found in {settings.AWS_REGION}")
                problems += 1
        except ClientError as e:
            print(f"{BAD} AMI check failed: {e}")
            problems += 1

    # 3. Security group exists
    if not settings.AWS_SECURITY_GROUP_ID:
        print(f"{WARN} AWS_SECURITY_GROUP_ID empty — instances launch into the default SG")
    else:
        try:
            sgs = ec2.describe_security_groups(GroupIds=[settings.AWS_SECURITY_GROUP_ID])["SecurityGroups"]
            sg = sgs[0]
            ports = set()
            for perm in sg.get("IpPermissions", []):
                fr, to = perm.get("FromPort"), perm.get("ToPort")
                if fr is not None:
                    ports.update(range(fr, (to or fr) + 1))
            has9000 = 9000 in ports
            mark = OK if has9000 else WARN
            print(f"{mark} Security group {settings.AWS_SECURITY_GROUP_ID} found ({sg['GroupName']}); "
                  f"inbound 9000 {'open' if has9000 else 'MISSING — master API unreachable'}")
            if not has9000:
                problems += 1
        except ClientError as e:
            print(f"{BAD} Security group check failed: {e}")
            problems += 1

    # 4. Key pair exists
    if not settings.AWS_KEY_PAIR_NAME:
        print(f"{WARN} AWS_KEY_PAIR_NAME empty — instances launch without SSH access (ok, but no debugging)")
    else:
        try:
            ec2.describe_key_pairs(KeyNames=[settings.AWS_KEY_PAIR_NAME])
            print(f"{OK} Key pair '{settings.AWS_KEY_PAIR_NAME}' found")
        except ClientError as e:
            print(f"{BAD} Key pair '{settings.AWS_KEY_PAIR_NAME}' not found: {e}")
            problems += 1

    # 5. Instance type sanity (just echo)
    print(f"{OK} Default instance type: {settings.AWS_INSTANCE_TYPE}")

    # 6. Can we actually launch? Dry-run RunInstances (no instance is created).
    try:
        params = {
            "ImageId": settings.AWS_AMI_ID,
            "InstanceType": settings.AWS_INSTANCE_TYPE,
            "MinCount": 1,
            "MaxCount": 1,
            "DryRun": True,
        }
        if settings.AWS_SECURITY_GROUP_ID:
            params["SecurityGroupIds"] = [settings.AWS_SECURITY_GROUP_ID]
        if settings.AWS_KEY_PAIR_NAME:
            params["KeyName"] = settings.AWS_KEY_PAIR_NAME
        ec2.run_instances(**params)
    except ClientError as e:
        if e.response["Error"]["Code"] == "DryRunOperation":
            print(f"{OK} RunInstances dry-run OK — the backend CAN launch instances")
        elif e.response["Error"]["Code"] in ("UnauthorizedOperation", "AccessDenied"):
            print(f"{BAD} RunInstances DENIED — IAM policy missing ec2:RunInstances: {e}")
            problems += 1
        elif "VcpuLimitExceeded" in str(e) or "InstanceLimitExceeded" in str(e):
            print(f"{WARN} vCPU/instance quota too low for {settings.AWS_INSTANCE_TYPE} — request a Service Quota increase")
            problems += 1
        else:
            print(f"{BAD} RunInstances dry-run failed: {e}")
            problems += 1

    print()
    if problems == 0:
        print(f"{OK} All checks passed — AWS is ready for Belleq provisioning.")
        return 0
    print(f"{BAD} {problems} problem(s) found — fix the lines marked ✗ above.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
