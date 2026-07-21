"""
LiteLLM AWS ECS Installer

Deploys LiteLLM Proxy to AWS ECS Fargate with:
- VPC networking (uses default VPC)
- Application Load Balancer
- RDS PostgreSQL for state storage
- Secrets Manager for master key and DB credentials
- CloudWatch Logs for observability

Install results (URL, Admin UI, master key) are written to
`.state-<stack>.json` next to this script (gitignored).

Usage:
    python install/installer.py deploy --region us-west-2 --stack-name litellm
    python install/installer.py destroy --region us-west-2 --stack-name litellm
    python install/installer.py status --region us-west-2 --stack-name litellm
"""

from __future__ import annotations

import argparse
import json
import secrets
import string
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.exceptions import ClientError


LITELLM_IMAGE = "ghcr.io/berriai/litellm:main-stable"
# Custom image with curl (Wolfi apk) for ECS container health checks.
# Built from install/Dockerfile and pushed to ECR as litellm:main-stable-curl.
LITELLM_IMAGE_WITH_CURL_TAG = "main-stable-curl"
LITELLM_PORT = 4000
DB_PORT = 5432
DB_NAME = "litellm"
DB_USER = "litellm"
INSTALL_DIR = Path(__file__).resolve().parent


def state_path(stack_name: str) -> Path:
    return INSTALL_DIR / f".state-{stack_name}.json"


def save_state(cfg: Config, *, alb_dns: str, master_key: str, **extra: object) -> Path:
    path = state_path(cfg.stack_name)
    payload = {
        "region": cfg.region,
        "stack_name": cfg.stack_name,
        "alb_dns": alb_dns,
        "url": f"http://{alb_dns}",
        "admin_ui": f"http://{alb_dns}/ui",
        "master_key": master_key,
        "cluster": f"{cfg.stack_name}-cluster",
        "service": f"{cfg.stack_name}-service",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        **extra,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def load_state(stack_name: str) -> dict | None:
    path = state_path(stack_name)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def is_stack_deployed(session, cfg: Config) -> bool:
    """True when ECS service is ACTIVE and ALB exists."""
    ecs = session.client("ecs")
    elbv2 = session.client("elbv2")
    cluster = f"{cfg.stack_name}-cluster"
    service = f"{cfg.stack_name}-service"
    try:
        services = ecs.describe_services(cluster=cluster, services=[service])["services"]
        active = [s for s in services if s.get("status") == "ACTIVE"]
        if not active:
            return False
    except ClientError:
        return False
    try:
        elbv2.describe_load_balancers(Names=[f"{cfg.stack_name}-alb"])
        return True
    except ClientError:
        return False


def fetch_endpoints(session, cfg: Config) -> dict[str, str]:
    elbv2 = session.client("elbv2")
    sm = session.client("secretsmanager")
    lb = elbv2.describe_load_balancers(Names=[f"{cfg.stack_name}-alb"])["LoadBalancers"][0]
    master_key = sm.get_secret_value(SecretId=f"{cfg.stack_name}/master-key")["SecretString"]
    return {"alb_dns": lb["DNSName"], "master_key": master_key, "alb_state": lb["State"]["Code"]}


def print_access_info(cfg: Config, alb_dns: str, master_key: str, state_file: Path | None = None) -> None:
    print(f"URL:        http://{alb_dns}")
    print(f"Admin UI:   http://{alb_dns}/ui")
    print(f"Master key: {master_key}")
    print(f"Region:     {cfg.region}")
    print(f"Stack:      {cfg.stack_name}")
    if state_file:
        print(f"State file: {state_file}")
    print("\nAdmin UI login:")
    print("  Username: admin")
    print("  Password: <Master key 위 값>")
    print("\nClient env:")
    print(f"  export LITELLM_URL=http://{alb_dns}")
    print(
        f'  export LITELLM_MASTER_KEY="$(aws secretsmanager get-secret-value '
        f'--secret-id {cfg.stack_name}/master-key --region {cfg.region} '
        f'--query SecretString --output text)"'
    )
    print(f"  export ANTHROPIC_BASE_URL=http://{alb_dns}")
    print('  export ANTHROPIC_AUTH_TOKEN="$LITELLM_MASTER_KEY"')


@dataclass
class Config:
    region: str
    stack_name: str
    cpu: str = "1024"
    memory: str = "2048"
    desired_count: int = 1
    db_instance_class: str = "db.t3.micro"
    db_allocated_storage: int = 20


def tag_spec(stack_name: str, resource_type: str) -> dict:
    return {
        "ResourceType": resource_type,
        "Tags": [
            {"Key": "Name", "Value": stack_name},
            {"Key": "ManagedBy", "Value": "litellm-installer"},
            {"Key": "Stack", "Value": stack_name},
        ],
    }


def random_password(length: int = 32) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def get_default_vpc(ec2) -> tuple[str, list[str]]:
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"]
    if not vpcs:
        raise RuntimeError("No default VPC found. Create one or extend installer to build a VPC.")
    vpc_id = vpcs[0]["VpcId"]
    subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"]
    subnet_ids = [s["SubnetId"] for s in subnets if s.get("MapPublicIpOnLaunch")]
    if len(subnet_ids) < 2:
        raise RuntimeError(f"Need at least 2 public subnets in default VPC {vpc_id}.")
    return vpc_id, subnet_ids


def ensure_security_group(ec2, name: str, description: str, vpc_id: str, stack_name: str) -> str:
    existing = ec2.describe_security_groups(
        Filters=[
            {"Name": "group-name", "Values": [name]},
            {"Name": "vpc-id", "Values": [vpc_id]},
        ]
    )["SecurityGroups"]
    if existing:
        return existing[0]["GroupId"]
    resp = ec2.create_security_group(
        GroupName=name,
        Description=description,
        VpcId=vpc_id,
        TagSpecifications=[tag_spec(stack_name, "security-group")],
    )
    return resp["GroupId"]


def authorize_ingress(ec2, sg_id: str, from_port: int, to_port: int, source: dict) -> None:
    try:
        ec2.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": from_port,
                    "ToPort": to_port,
                    **source,
                }
            ],
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "InvalidPermission.Duplicate":
            raise


def setup_networking(session, cfg: Config) -> dict:
    ec2 = session.client("ec2")
    vpc_id, subnet_ids = get_default_vpc(ec2)

    alb_sg = ensure_security_group(
        ec2, f"{cfg.stack_name}-alb-sg", "LiteLLM ALB", vpc_id, cfg.stack_name
    )
    task_sg = ensure_security_group(
        ec2, f"{cfg.stack_name}-task-sg", "LiteLLM ECS task", vpc_id, cfg.stack_name
    )
    db_sg = ensure_security_group(
        ec2, f"{cfg.stack_name}-db-sg", "LiteLLM RDS", vpc_id, cfg.stack_name
    )

    authorize_ingress(ec2, alb_sg, 80, 80, {"IpRanges": [{"CidrIp": "0.0.0.0/0"}]})
    authorize_ingress(
        ec2, task_sg, LITELLM_PORT, LITELLM_PORT, {"UserIdGroupPairs": [{"GroupId": alb_sg}]}
    )
    authorize_ingress(
        ec2, db_sg, DB_PORT, DB_PORT, {"UserIdGroupPairs": [{"GroupId": task_sg}]}
    )

    return {
        "vpc_id": vpc_id,
        "subnet_ids": subnet_ids,
        "alb_sg": alb_sg,
        "task_sg": task_sg,
        "db_sg": db_sg,
    }


def ensure_secret(sm, name: str, value: str, stack_name: str) -> tuple[str, str]:
    """Create secret or return existing. Returns (arn, secret_string)."""
    try:
        resp = sm.create_secret(
            Name=name,
            SecretString=value,
            Tags=[
                {"Key": "Stack", "Value": stack_name},
                {"Key": "ManagedBy", "Value": "litellm-installer"},
            ],
        )
        return resp["ARN"], value
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceExistsException":
            arn = sm.describe_secret(SecretId=name)["ARN"]
            existing = sm.get_secret_value(SecretId=name)["SecretString"]
            return arn, existing
        raise


def setup_secrets(session, cfg: Config) -> dict:
    sm = session.client("secretsmanager")
    master_key = "sk-" + random_password(40)
    db_password = random_password(24)

    master_arn, master_key = ensure_secret(
        sm, f"{cfg.stack_name}/master-key", master_key, cfg.stack_name
    )
    db_arn, db_password = ensure_secret(
        sm, f"{cfg.stack_name}/db-password", db_password, cfg.stack_name
    )

    return {
        "master_key_arn": master_arn,
        "db_password_arn": db_arn,
        "db_password": db_password,
        "master_key": master_key,
    }


def setup_database(session, cfg: Config, net: dict, secrets_out: dict) -> str:
    rds = session.client("rds")
    subnet_group_name = f"{cfg.stack_name}-db-subnets"

    try:
        rds.create_db_subnet_group(
            DBSubnetGroupName=subnet_group_name,
            DBSubnetGroupDescription="LiteLLM DB subnets",
            SubnetIds=net["subnet_ids"],
            Tags=[{"Key": "Stack", "Value": cfg.stack_name}],
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "DBSubnetGroupAlreadyExists":
            raise

    db_id = f"{cfg.stack_name}-db"
    try:
        rds.create_db_instance(
            DBInstanceIdentifier=db_id,
            DBName=DB_NAME,
            Engine="postgres",
            EngineVersion="16.14",
            DBInstanceClass=cfg.db_instance_class,
            AllocatedStorage=cfg.db_allocated_storage,
            MasterUsername=DB_USER,
            MasterUserPassword=secrets_out["db_password"],
            VpcSecurityGroupIds=[net["db_sg"]],
            DBSubnetGroupName=subnet_group_name,
            PubliclyAccessible=False,
            BackupRetentionPeriod=7,
            StorageEncrypted=True,
            Tags=[{"Key": "Stack", "Value": cfg.stack_name}],
        )
        print(f"Creating RDS instance {db_id} (this may take 5-10 minutes)...")
    except ClientError as e:
        if e.response["Error"]["Code"] != "DBInstanceAlreadyExists":
            raise
        print(f"RDS instance {db_id} already exists.")

    waiter = rds.get_waiter("db_instance_available")
    waiter.wait(DBInstanceIdentifier=db_id, WaiterConfig={"Delay": 30, "MaxAttempts": 40})

    resp = rds.describe_db_instances(DBInstanceIdentifier=db_id)
    endpoint = resp["DBInstances"][0]["Endpoint"]
    return f"postgresql://{DB_USER}:{secrets_out['db_password']}@{endpoint['Address']}:{endpoint['Port']}/{DB_NAME}"


def ensure_role(iam, role_name: str, assume_policy: dict, stack_name: str) -> str:
    try:
        resp = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(assume_policy),
            Tags=[{"Key": "Stack", "Value": stack_name}],
        )
        return resp["Role"]["Arn"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "EntityAlreadyExists":
            return iam.get_role(RoleName=role_name)["Role"]["Arn"]
        raise


def setup_iam(session, cfg: Config, secrets_out: dict) -> dict:
    iam = session.client("iam")
    assume = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }

    exec_role_name = f"{cfg.stack_name}-ecs-exec-role"
    exec_arn = ensure_role(iam, exec_role_name, assume, cfg.stack_name)
    iam.attach_role_policy(
        RoleName=exec_role_name,
        PolicyArn="arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy",
    )

    secrets_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["secretsmanager:GetSecretValue"],
                "Resource": [secrets_out["master_key_arn"], secrets_out["db_password_arn"]],
            }
        ],
    }
    iam.put_role_policy(
        RoleName=exec_role_name,
        PolicyName="LiteLLMSecretsAccess",
        PolicyDocument=json.dumps(secrets_policy),
    )

    task_role_name = f"{cfg.stack_name}-ecs-task-role"
    task_arn = ensure_role(iam, task_role_name, assume, cfg.stack_name)

    time.sleep(10)
    return {"exec_role_arn": exec_arn, "task_role_arn": task_arn}


def setup_load_balancer(session, cfg: Config, net: dict) -> dict:
    elbv2 = session.client("elbv2")

    lbs = elbv2.describe_load_balancers()["LoadBalancers"]
    lb = next((x for x in lbs if x["LoadBalancerName"] == f"{cfg.stack_name}-alb"), None)
    if not lb:
        lb = elbv2.create_load_balancer(
            Name=f"{cfg.stack_name}-alb",
            Subnets=net["subnet_ids"],
            SecurityGroups=[net["alb_sg"]],
            Scheme="internet-facing",
            Type="application",
            IpAddressType="ipv4",
            Tags=[{"Key": "Stack", "Value": cfg.stack_name}],
        )["LoadBalancers"][0]
    lb_arn = lb["LoadBalancerArn"]
    lb_dns = lb["DNSName"]

    tgs = elbv2.describe_target_groups()["TargetGroups"]
    tg = next((x for x in tgs if x["TargetGroupName"] == f"{cfg.stack_name}-tg"), None)
    if not tg:
        tg = elbv2.create_target_group(
            Name=f"{cfg.stack_name}-tg",
            Protocol="HTTP",
            Port=LITELLM_PORT,
            VpcId=net["vpc_id"],
            TargetType="ip",
            HealthCheckPath="/health/liveliness",
            HealthCheckIntervalSeconds=30,
            HealthCheckTimeoutSeconds=10,
            HealthyThresholdCount=2,
            UnhealthyThresholdCount=3,
            Matcher={"HttpCode": "200"},
        )["TargetGroups"][0]
    tg_arn = tg["TargetGroupArn"]

    listeners = elbv2.describe_listeners(LoadBalancerArn=lb_arn)["Listeners"]
    if not any(l["Port"] == 80 for l in listeners):
        elbv2.create_listener(
            LoadBalancerArn=lb_arn,
            Protocol="HTTP",
            Port=80,
            DefaultActions=[{"Type": "forward", "TargetGroupArn": tg_arn}],
        )

    return {"lb_arn": lb_arn, "lb_dns": lb_dns, "tg_arn": tg_arn}


def setup_log_group(session, cfg: Config) -> str:
    logs = session.client("logs")
    name = f"/ecs/{cfg.stack_name}"
    try:
        logs.create_log_group(logGroupName=name, tags={"Stack": cfg.stack_name})
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceAlreadyExistsException":
            raise
    return name


def resolve_litellm_image(session, cfg: Config) -> str:
    """Prefer ECR image with curl; fall back to upstream ghcr image."""
    account = session.client("sts").get_caller_identity()["Account"]
    ecr_uri = (
        f"{account}.dkr.ecr.{cfg.region}.amazonaws.com/"
        f"{cfg.stack_name}:{LITELLM_IMAGE_WITH_CURL_TAG}"
    )
    ecr = session.client("ecr")
    try:
        ecr.describe_images(
            repositoryName=cfg.stack_name,
            imageIds=[{"imageTag": LITELLM_IMAGE_WITH_CURL_TAG}],
        )
        print(f"  Using ECR image with curl: {ecr_uri}")
        return ecr_uri
    except ClientError:
        print(f"  ECR image not found; using upstream: {LITELLM_IMAGE}")
        return LITELLM_IMAGE


def register_task_definition(
    session, cfg: Config, iam_out: dict, secrets_out: dict, db_url: str, log_group: str
) -> str:
    ecs = session.client("ecs")
    image = resolve_litellm_image(session, cfg)
    container_def = {
        "name": "litellm",
        "image": image,
        "essential": True,
        "portMappings": [{"containerPort": LITELLM_PORT, "protocol": "tcp"}],
        "environment": [
            {"name": "DATABASE_URL", "value": db_url},
            {"name": "STORE_MODEL_IN_DB", "value": "True"},
            {"name": "PORT", "value": str(LITELLM_PORT)},
        ],
        "secrets": [
            {"name": "LITELLM_MASTER_KEY", "valueFrom": secrets_out["master_key_arn"]},
        ],
        "logConfiguration": {
            "logDriver": "awslogs",
            "options": {
                "awslogs-group": log_group,
                "awslogs-region": cfg.region,
                "awslogs-stream-prefix": "litellm",
            },
        },
        "healthCheck": {
            "command": [
                "CMD-SHELL",
                f"curl -f http://localhost:{LITELLM_PORT}/health/liveliness || exit 1",
            ],
            "interval": 30,
            "timeout": 10,
            "retries": 5,
            "startPeriod": 120,
        },
    }
    resp = ecs.register_task_definition(
        family=f"{cfg.stack_name}-task",
        networkMode="awsvpc",
        requiresCompatibilities=["FARGATE"],
        cpu=cfg.cpu,
        memory=cfg.memory,
        executionRoleArn=iam_out["exec_role_arn"],
        taskRoleArn=iam_out["task_role_arn"],
        containerDefinitions=[container_def],
        tags=[{"key": "Stack", "value": cfg.stack_name}],
    )
    return resp["taskDefinition"]["taskDefinitionArn"]


def ensure_ecs_service_linked_role(session) -> None:
    """Ensure AWSServiceRoleForECS exists (required for CreateCluster)."""
    iam = session.client("iam")
    try:
        iam.create_service_linked_role(AWSServiceName="ecs.amazonaws.com")
        print("Created ECS service-linked role; waiting for IAM propagation...")
        time.sleep(10)
    except ClientError as e:
        code = e.response["Error"]["Code"]
        # Role already present in this account
        if code in ("InvalidInput", "EntityAlreadyExists"):
            return
        raise


def setup_ecs(session, cfg: Config, net: dict, lb_out: dict, task_def_arn: str) -> str:
    ensure_ecs_service_linked_role(session)
    ecs = session.client("ecs")
    cluster_name = f"{cfg.stack_name}-cluster"
    try:
        ecs.create_cluster(
            clusterName=cluster_name,
            capacityProviders=["FARGATE"],
            tags=[{"key": "Stack", "value": cfg.stack_name}],
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ClusterAlreadyExistsException":
            raise
        print(f"ECS cluster {cluster_name} already exists.")

    service_name = f"{cfg.stack_name}-service"
    existing = ecs.describe_services(cluster=cluster_name, services=[service_name])["services"]
    active = [s for s in existing if s["status"] == "ACTIVE"]

    network_config = {
        "awsvpcConfiguration": {
            "subnets": net["subnet_ids"],
            "securityGroups": [net["task_sg"]],
            "assignPublicIp": "ENABLED",
        }
    }

    if active:
        ecs.update_service(
            cluster=cluster_name,
            service=service_name,
            taskDefinition=task_def_arn,
            desiredCount=cfg.desired_count,
            networkConfiguration=network_config,
        )
        print(f"Updated ECS service {service_name}.")
    else:
        ecs.create_service(
            cluster=cluster_name,
            serviceName=service_name,
            taskDefinition=task_def_arn,
            desiredCount=cfg.desired_count,
            launchType="FARGATE",
            networkConfiguration=network_config,
            loadBalancers=[
                {
                    "targetGroupArn": lb_out["tg_arn"],
                    "containerName": "litellm",
                    "containerPort": LITELLM_PORT,
                }
            ],
            healthCheckGracePeriodSeconds=120,
            tags=[{"key": "Stack", "value": cfg.stack_name}],
        )
        print(f"Created ECS service {service_name}.")
    return cluster_name


def deploy(cfg: Config) -> None:
    session = boto3.Session(region_name=cfg.region)

    if is_stack_deployed(session, cfg):
        print(
            f"Stack '{cfg.stack_name}' already deployed in {cfg.region}. "
            "Skipping create; refreshing state file."
        )
        ep = fetch_endpoints(session, cfg)
        path = save_state(cfg, alb_dns=ep["alb_dns"], master_key=ep["master_key"])
        print("\n" + "=" * 60)
        print("Existing LiteLLM stack (no changes).")
        print("=" * 60)
        print_access_info(cfg, ep["alb_dns"], ep["master_key"], path)
        print("\nRegistering default models...")
        _register_default_models(cfg)
        return

    print(f"[1/7] Setting up networking...")
    net = setup_networking(session, cfg)

    print(f"[2/7] Creating secrets...")
    secrets_out = setup_secrets(session, cfg)

    print(f"[3/7] Provisioning RDS PostgreSQL...")
    db_url = setup_database(session, cfg, net, secrets_out)

    print(f"[4/7] Setting up IAM roles...")
    iam_out = setup_iam(session, cfg, secrets_out)

    print(f"[5/7] Creating ALB and target group...")
    lb_out = setup_load_balancer(session, cfg, net)

    print(f"[6/7] Registering task definition...")
    log_group = setup_log_group(session, cfg)
    task_def_arn = register_task_definition(session, cfg, iam_out, secrets_out, db_url, log_group)

    print(f"[7/7] Creating ECS cluster and service...")
    setup_ecs(session, cfg, net, lb_out, task_def_arn)

    master_key = secrets_out.get("master_key") or session.client("secretsmanager").get_secret_value(
        SecretId=f"{cfg.stack_name}/master-key"
    )["SecretString"]

    path = save_state(cfg, alb_dns=lb_out["lb_dns"], master_key=master_key)

    print("\n" + "=" * 60)
    print("LiteLLM deployment complete.")
    print("=" * 60)
    print_access_info(cfg, lb_out["lb_dns"], master_key, path)
    print("\nNote: Wait 2-3 minutes for ECS tasks to become healthy.")
    print("\nRegistering default models...")
    _register_default_models(cfg)


def _register_default_models(cfg: Config) -> None:
    """Best-effort: register Bedrock Claude (+ OpenAI if key present)."""
    install_dir = str(Path(__file__).resolve().parent)
    if install_dir not in sys.path:
        sys.path.insert(0, install_dir)
    try:
        from register_models import register_default_models

        register_default_models(region=cfg.region, stack_name=cfg.stack_name)
    except Exception as e:
        print(f"Model registration skipped/failed: {e}")
        print("  Re-run: python install/register_models.py")


def destroy(cfg: Config) -> None:
    """Delegate to uninstaller.py (same resource set as deploy)."""
    install_dir = str(Path(__file__).resolve().parent)
    if install_dir not in sys.path:
        sys.path.insert(0, install_dir)
    from uninstaller import Config as UConfig
    from uninstaller import uninstall_stack

    uninstall_stack(
        UConfig(region=cfg.region, stack_name=cfg.stack_name),
        dry_run=False,
        keep_state=False,
    )


def status(cfg: Config) -> None:
    session = boto3.Session(region_name=cfg.region)
    ecs = session.client("ecs")

    cluster = f"{cfg.stack_name}-cluster"
    service = f"{cfg.stack_name}-service"
    try:
        svc = ecs.describe_services(cluster=cluster, services=[service])["services"][0]
        print(f"Service:  {svc['status']}")
        print(
            f"Desired:  {svc['desiredCount']}   "
            f"Running: {svc['runningCount']}   "
            f"Pending: {svc['pendingCount']}"
        )
    except (ClientError, IndexError):
        print(f"Service {service} not found.")

    try:
        ep = fetch_endpoints(session, cfg)
    except (ClientError, IndexError, KeyError):
        print("ALB or master-key secret not found.")
        local = load_state(cfg.stack_name)
        if local:
            print(f"(local state) URL: {local.get('url')}  Admin UI: {local.get('admin_ui')}")
        return

    print(f"ALB:      {ep['alb_state']}")
    path = save_state(cfg, alb_dns=ep["alb_dns"], master_key=ep["master_key"])
    print_access_info(cfg, ep["alb_dns"], ep["master_key"], path)


def main() -> int:
    parser = argparse.ArgumentParser(description="LiteLLM AWS ECS installer")
    parser.add_argument("action", choices=["deploy", "destroy", "status"])
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--stack-name", default="litellm")
    parser.add_argument("--cpu", default="1024")
    parser.add_argument("--memory", default="2048")
    parser.add_argument("--desired-count", type=int, default=1)
    parser.add_argument("--db-instance-class", default="db.t3.micro")
    args = parser.parse_args()

    cfg = Config(
        region=args.region,
        stack_name=args.stack_name,
        cpu=args.cpu,
        memory=args.memory,
        desired_count=args.desired_count,
        db_instance_class=args.db_instance_class,
    )

    if args.action == "deploy":
        deploy(cfg)
    elif args.action == "destroy":
        confirm = input(f"Destroy stack '{cfg.stack_name}' in {cfg.region}? [y/N]: ")
        if confirm.lower() == "y":
            destroy(cfg)
        else:
            print("Aborted.")
    elif args.action == "status":
        status(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
