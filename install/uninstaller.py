#!/usr/bin/env python3
"""LiteLLM uninstaller — remove all AWS resources created by installer.py.

Deletes (in order):
  ECS service/cluster → ALB/listener/TG → RDS + subnet group
  → Secrets → IAM roles → Security groups → CloudWatch log group
  → local install/.state-<stack>.json

Usage:
  python install/uninstaller.py --region us-west-2 --stack-name litellm --yes
  python install/uninstaller.py --region us-west-2 --stack-name litellm --dry-run
  python install/uninstaller.py --yes --keep-state
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

INSTALL_DIR = Path(__file__).resolve().parent


@dataclass
class Config:
    region: str
    stack_name: str


def state_path(stack_name: str) -> Path:
    return INSTALL_DIR / f".state-{stack_name}.json"


def _ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def _skip(msg: str) -> None:
    print(f"  - {msg}")


def _warn(msg: str) -> None:
    print(f"  ! {msg}")


def _call(dry_run: bool, desc: str, fn, *args, **kwargs):
    if dry_run:
        print(f"  [dry-run] {desc}")
        return None
    try:
        result = fn(*args, **kwargs)
        _ok(desc)
        return result
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in (
            "ClusterNotFoundException",
            "ServiceNotFoundException",
            "LoadBalancerNotFound",
            "TargetGroupNotFound",
            "DBInstanceNotFound",
            "DBSubnetGroupNotFoundFault",
            "ResourceNotFoundException",
            "NoSuchEntity",
            "InvalidGroup.NotFound",
            "ResourceNotFoundException",
        ):
            _skip(f"{desc} (not found)")
            return None
        _warn(f"{desc}: {e}")
        return None
    except Exception as e:
        _warn(f"{desc}: {e}")
        return None


def _wait_service_gone(ecs, cluster: str, service: str, dry_run: bool, timeout: int = 300) -> None:
    if dry_run:
        return
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            svcs = ecs.describe_services(cluster=cluster, services=[service])["services"]
            if not svcs or svcs[0].get("status") == "INACTIVE":
                return
        except ClientError:
            return
        time.sleep(5)


def _wait_alb_gone(elbv2, name: str, dry_run: bool, timeout: int = 180) -> None:
    if dry_run:
        return
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            elbv2.describe_load_balancers(Names=[name])
            time.sleep(5)
        except ClientError:
            return


def _wait_rds_gone(rds, db_id: str, dry_run: bool) -> None:
    if dry_run:
        return
    try:
        rds.get_waiter("db_instance_deleted").wait(
            DBInstanceIdentifier=db_id,
            WaiterConfig={"Delay": 30, "MaxAttempts": 40},
        )
        _ok(f"RDS {db_id} deleted")
    except Exception as e:
        _warn(f"wait RDS delete: {e}")


def _revoke_all_ingress(ec2, sg_id: str, dry_run: bool) -> None:
    if dry_run:
        print(f"  [dry-run] revoke ingress on {sg_id}")
        return
    try:
        sg = ec2.describe_security_groups(GroupIds=[sg_id])["SecurityGroups"][0]
        perms = sg.get("IpPermissions") or []
        if perms:
            ec2.revoke_security_group_ingress(GroupId=sg_id, IpPermissions=perms)
            _ok(f"revoked ingress {sg_id}")
    except ClientError as e:
        _warn(f"revoke {sg_id}: {e}")


def _delete_task_definitions(ecs, family: str, dry_run: bool) -> None:
    try:
        arns: list[str] = []
        for status in ("ACTIVE", "INACTIVE"):
            paginator = ecs.get_paginator("list_task_definitions")
            for page in paginator.paginate(familyPrefix=family, status=status):
                arns.extend(page.get("taskDefinitionArns") or [])
    except ClientError as e:
        _warn(f"list task defs: {e}")
        return

    # Only deregister ACTIVE; INACTIVE revisions remain but family is unused
    for arn in arns:
        if dry_run:
            print(f"  [dry-run] deregister {arn}")
            continue
        try:
            ecs.deregister_task_definition(taskDefinition=arn)
            _ok(f"deregister {arn.split('/')[-1]}")
        except ClientError as e:
            _warn(f"deregister {arn}: {e}")


def uninstall_stack(
    cfg: Config,
    *,
    dry_run: bool = False,
    keep_state: bool = False,
) -> None:
    """Remove every resource created by install/installer.py for this stack."""
    session = boto3.Session(region_name=cfg.region)
    ecs = session.client("ecs")
    elbv2 = session.client("elbv2")
    rds = session.client("rds")
    sm = session.client("secretsmanager")
    iam = session.client("iam")
    ec2 = session.client("ec2")
    logs = session.client("logs")

    stack = cfg.stack_name
    cluster = f"{stack}-cluster"
    service = f"{stack}-service"
    alb_name = f"{stack}-alb"
    tg_name = f"{stack}-tg"
    db_id = f"{stack}-db"
    subnet_group = f"{stack}-db-subnets"
    log_group = f"/ecs/{stack}"

    print(f"Uninstalling LiteLLM stack '{stack}' in {cfg.region}"
          + (" [DRY-RUN]" if dry_run else ""))
    print("=" * 60)

    # 1) ECS
    print("\n[1/8] ECS service & cluster")
    _call(
        dry_run,
        f"scale {service} → 0",
        ecs.update_service,
        cluster=cluster,
        service=service,
        desiredCount=0,
    )
    _call(
        dry_run,
        f"delete service {service}",
        ecs.delete_service,
        cluster=cluster,
        service=service,
        force=True,
    )
    _wait_service_gone(ecs, cluster, service, dry_run)
    _delete_task_definitions(ecs, f"{stack}-task", dry_run)
    _call(dry_run, f"delete cluster {cluster}", ecs.delete_cluster, cluster=cluster)

    # 2) ALB
    print("\n[2/8] ALB, listeners, target group")
    try:
        lbs = elbv2.describe_load_balancers(Names=[alb_name])["LoadBalancers"]
    except ClientError:
        lbs = []
        _skip(f"ALB {alb_name} (not found)")

    for lb in lbs:
        lb_arn = lb["LoadBalancerArn"]
        try:
            for listener in elbv2.describe_listeners(LoadBalancerArn=lb_arn).get("Listeners", []):
                _call(
                    dry_run,
                    f"delete listener :{listener.get('Port')}",
                    elbv2.delete_listener,
                    ListenerArn=listener["ListenerArn"],
                )
        except ClientError as e:
            _warn(f"list listeners: {e}")
        _call(dry_run, f"delete ALB {alb_name}", elbv2.delete_load_balancer, LoadBalancerArn=lb_arn)

    _wait_alb_gone(elbv2, alb_name, dry_run)

    try:
        tgs = elbv2.describe_target_groups(Names=[tg_name])["TargetGroups"]
    except ClientError:
        tgs = []
        _skip(f"TG {tg_name} (not found)")
    for tg in tgs:
        _call(
            dry_run,
            f"delete target group {tg_name}",
            elbv2.delete_target_group,
            TargetGroupArn=tg["TargetGroupArn"],
        )

    # 3) RDS
    print("\n[3/8] RDS PostgreSQL")
    _call(
        dry_run,
        f"delete DB instance {db_id}",
        rds.delete_db_instance,
        DBInstanceIdentifier=db_id,
        SkipFinalSnapshot=True,
        DeleteAutomatedBackups=True,
    )
    if not dry_run:
        # Only wait if instance existed / delete was accepted
        try:
            rds.describe_db_instances(DBInstanceIdentifier=db_id)
            print("  … waiting for RDS deletion (several minutes)…")
            _wait_rds_gone(rds, db_id, dry_run)
        except ClientError:
            _skip(f"RDS {db_id} already gone")
    _call(
        dry_run,
        f"delete DB subnet group {subnet_group}",
        rds.delete_db_subnet_group,
        DBSubnetGroupName=subnet_group,
    )

    # 4) Secrets
    print("\n[4/8] Secrets Manager")
    for suffix in ("master-key", "db-password"):
        sid = f"{stack}/{suffix}"
        _call(
            dry_run,
            f"delete secret {sid}",
            sm.delete_secret,
            SecretId=sid,
            ForceDeleteWithoutRecovery=True,
        )

    # 5) IAM
    print("\n[5/8] IAM roles")
    for role in (f"{stack}-ecs-exec-role", f"{stack}-ecs-task-role"):
        if dry_run:
            print(f"  [dry-run] delete role {role} (detach policies first)")
            continue
        try:
            for p in iam.list_attached_role_policies(RoleName=role).get("AttachedPolicies", []):
                iam.detach_role_policy(RoleName=role, PolicyArn=p["PolicyArn"])
            for p in iam.list_role_policies(RoleName=role).get("PolicyNames", []):
                iam.delete_role_policy(RoleName=role, PolicyName=p)
            iam.delete_role(RoleName=role)
            _ok(f"delete role {role}")
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "NoSuchEntity":
                _skip(f"role {role} (not found)")
            else:
                _warn(f"role {role}: {e}")

    # 6) Security groups (revoke cross-refs, then delete)
    print("\n[6/8] Security groups")
    sg_names = (f"{stack}-alb-sg", f"{stack}-task-sg", f"{stack}-db-sg")
    sg_ids: list[str] = []
    for name in sg_names:
        try:
            sgs = ec2.describe_security_groups(
                Filters=[{"Name": "group-name", "Values": [name]}]
            )["SecurityGroups"]
            for sg in sgs:
                sg_ids.append(sg["GroupId"])
        except ClientError:
            pass

    for sg_id in sg_ids:
        _revoke_all_ingress(ec2, sg_id, dry_run)

    # Delete task/db first (depend on alb/task), then alb
    for name in (f"{stack}-db-sg", f"{stack}-task-sg", f"{stack}-alb-sg"):
        try:
            sgs = ec2.describe_security_groups(
                Filters=[{"Name": "group-name", "Values": [name]}]
            )["SecurityGroups"]
        except ClientError:
            sgs = []
        for sg in sgs:
            _call(
                dry_run,
                f"delete SG {name} ({sg['GroupId']})",
                ec2.delete_security_group,
                GroupId=sg["GroupId"],
            )

    # 7) Logs
    print("\n[7/8] CloudWatch Logs")
    _call(dry_run, f"delete log group {log_group}", logs.delete_log_group, logGroupName=log_group)

    # 8) Local state
    print("\n[8/8] Local state file")
    path = state_path(stack)
    if keep_state:
        _skip(f"keep {path}")
    elif path.is_file():
        if dry_run:
            print(f"  [dry-run] remove {path}")
        else:
            path.unlink()
            _ok(f"removed {path}")
    else:
        _skip(f"{path} (not present)")

    print("\n" + "=" * 60)
    if dry_run:
        print("Dry-run complete. Re-run with --yes to delete.")
    else:
        print(f"Stack '{stack}' fully uninstalled.")
    print("Default VPC / subnets were not deleted (account shared).")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Remove all LiteLLM resources created by install/installer.py",
    )
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--stack-name", default="litellm")
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Confirm destructive uninstall (required unless --dry-run)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print actions without deleting",
    )
    parser.add_argument(
        "--keep-state",
        action="store_true",
        help="Keep install/.state-<stack>.json after uninstall",
    )
    args = parser.parse_args(argv)

    cfg = Config(region=args.region, stack_name=args.stack_name)

    if not args.dry_run and not args.yes:
        print(
            f"This will permanently delete stack '{cfg.stack_name}' in {cfg.region}\n"
            f"(ECS, ALB, RDS, Secrets, IAM, SGs, logs, state file).\n"
            f"Re-run with --yes to confirm, or --dry-run to preview."
        )
        return 1

    if args.yes and not args.dry_run:
        confirm = input(
            f"Type the stack name '{cfg.stack_name}' to confirm uninstall: "
        ).strip()
        if confirm != cfg.stack_name:
            print("Aborted (name mismatch).")
            return 1

    uninstall_stack(cfg, dry_run=args.dry_run, keep_state=args.keep_state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
