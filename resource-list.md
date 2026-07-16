# LiteLLM Installer - 설치되는 AWS 인프라 리스트

`install/installer.py`로 배포되는 모든 AWS 리소스의 상세 목록입니다.

## 총 리소스 개수

- **보안 (Security Groups)**: 3개
- **네트워킹 (VPC/Subnet)**: 기존 default VPC 재사용
- **데이터베이스 (RDS)**: 1개 + 1개 (DB Subnet Group)
- **시크릿 관리 (Secrets Manager)**: 2개
- **IAM 역할**: 2개 + 3개 정책
- **로드 밸런서 (ELBv2)**: 1개 ALB + 1개 Target Group + 1개 Listener
- **로깅**: 1개 CloudWatch Log Group
- **ECS**: 1개 Cluster + 1개 Service + 1개 Task Definition
- **합계**: ~15개의 주요 리소스

---

## 단계별 생성 리소스

### 1단계: 네트워킹 (`setup_networking`)

#### Security Groups

| 이름 | ID 패턴 | 설명 | 인바운드 규칙 |
|-----|---------|------|-------------|
| `{stack}-alb-sg` | `sg-xxxxx` | ALB용 보안 그룹 | 포트 80 (모든 IP: `0.0.0.0/0`) |
| `{stack}-task-sg` | `sg-xxxxx` | ECS Task용 보안 그룹 | 포트 4000 (ALB SG로부터) |
| `{stack}-db-sg` | `sg-xxxxx` | RDS DB용 보안 그룹 | 포트 5432 (Task SG로부터) |

**VPC & Subnets**: Default VPC + public subnet 2개 이상 (기존 리소스)

#### 아웃바운드 흐름

```
인터넷 ──:80──► ALB SG ──:4000──► Task SG ──:5432──► RDS SG
```

---

### 2단계: 시크릿 관리 (`setup_secrets`)

#### Secrets Manager

| 이름 | 형식 | 용도 |
|-----|------|------|
| `{stack}/master-key` | `sk-` + 40자 랜덤 | LiteLLM API Bearer 토큰 + Admin UI 비밀번호 |
| `{stack}/db-password` | 24자 랜덤 | RDS PostgreSQL 마스터 비밀번호 |

**특징**:
- 자동 생성 (이미 있으면 기존 값 재사용)
- Tag: `Stack={stack_name}`, `ManagedBy=litellm-installer`

---

### 3단계: 데이터베이스 (`setup_database`)

#### RDS PostgreSQL

| 항목 | 값 |
|-----|-----|
| **인스턴스 식별자** | `{stack}-db` |
| **엔진** | PostgreSQL 16.14 |
| **인스턴스 클래스** | `db.t3.micro` (기본) / `--db-instance-class`로 변경 가능 |
| **할당 스토리지** | `20` GB (기본) / `--db-allocated-storage`로 변경 가능 |
| **DB 이름** | `litellm` |
| **마스터 사용자** | `litellm` |
| **마스터 비밀번호** | Secrets Manager `{stack}/db-password` 에서 주입 |

#### RDS 설정

| 설정 | 값 |
|-----|-----|
| **VPC 보안 그룹** | `{stack}-db-sg` |
| **DB Subnet Group** | `{stack}-db-subnets` (2+ 서브넷) |
| **공개 액세스** | 비활성화 (`PubliclyAccessible=False`) |
| **백업 보존 기간** | 7일 |
| **스토리지 암호화** | 활성화 |
| **포트** | 5432 |

**대기 시간**: 생성에 약 5–10분 소요 (waiter 사용)

---

### 4단계: IAM 역할 (`setup_iam`)

#### Execution Role: `{stack}-ecs-exec-role`

**용도**: ECS Task가 Secrets Manager, ECR에서 시크릿과 이미지 가져오기

**연결 정책**:
1. 관리형 정책: `AmazonECSTaskExecutionRolePolicy`
2. 커스텀 인라인 정책: `LiteLLMSecretsAccess`
   - Action: `secretsmanager:GetSecretValue`
   - Resource: 마스터 키 ARN + DB 비밀번호 ARN

#### Task Role: `{stack}-ecs-task-role`

**용도**: ECS Task 실행 중 AWS 서비스 호출 (Bedrock InvokeModel 등)

**정책**: 기본 없음 (필요시 운영 중 추가)
- 예: Bedrock InvokeModel 권한 추가 시 이 역할에 붙임

**assume 정책** (둘 다 동일):
```json
{
  "Effect": "Allow",
  "Principal": { "Service": "ecs-tasks.amazonaws.com" },
  "Action": "sts:AssumeRole"
}
```

---

### 5단계: 로드 밸런서 (`setup_load_balancer`)

#### Application Load Balancer (ALB)

| 항목 | 값 |
|-----|-----|
| **이름** | `{stack}-alb` |
| **타입** | Application Load Balancer |
| **스킴** | internet-facing |
| **IP 주소 타입** | ipv4 |
| **보안 그룹** | `{stack}-alb-sg` |
| **서브넷** | default VPC public 서브넷들 |
| **DNS 이름** | `{stack}-alb-<region>.elb.amazonaws.com` |

#### Target Group

| 항목 | 값 |
|-----|-----|
| **이름** | `{stack}-tg` |
| **프로토콜** | HTTP |
| **포트** | 4000 (LITELLM_PORT) |
| **VPC ID** | default VPC |
| **타겟 타입** | IP (Fargate용) |
| **헬스 체크 경로** | `/health/liveliness` |
| **헬스 체크 인터벌** | 30초 |
| **헬스 체크 타임아웃** | 10초 |
| **정상 임계값** | 2 |
| **비정상 임계값** | 3 |
| **매칭 HTTP 코드** | 200 |

#### Listener

| 항목 | 값 |
|-----|-----|
| **프로토콜** | HTTP |
| **포트** | 80 |
| **기본 동작** | Target Group `{stack}-tg` 로 포워드 |

---

### 6단계: 로깅 (`setup_log_group`)

#### CloudWatch Log Group

| 항목 | 값 |
|-----|-----|
| **이름** | `/ecs/{stack}-task` |
| **보존 기간** | 7일 (기본) / `--log-retention-days`로 변경 가능 |

---

### 7단계: ECS (`register_task_definition` + `setup_ecs`)

#### Task Definition

| 항목 | 값 |
|-----|-----|
| **패밀리명** | `{stack}-task` |
| **CPU** | 1024 (기본) / `--cpu`로 변경 |
| **메모리** | 2048 MB (기본) / `--memory`로 변경 |
| **네트워크 모드** | awsvpc (Fargate) |
| **호환성** | FARGATE |
| **Execution Role** | `{stack}-ecs-exec-role` ARN |
| **Task Role** | `{stack}-ecs-task-role` ARN |

#### Container Definition

| 항목 | 값 |
|-----|-----|
| **이름** | `litellm` |
| **이미지** | `ghcr.io/berriai/litellm:main-stable` |
| **포트** | 4000 (TCP) |
| **필수** | true |

**환경 변수**:
```
DATABASE_URL=postgresql://litellm:PASSWORD@RDS-ENDPOINT:5432/litellm
STORE_MODEL_IN_DB=True
PORT=4000
```

**시크릿 (Secrets Manager 주입)**:
```
LITELLM_MASTER_KEY  ← {stack}/master-key ARN
```

**로깅**:
- Driver: `awslogs`
- Log Group: `/ecs/{stack}-task`
- Stream Prefix: `litellm`
- Region: 배포 리전

**Health Check**:
```
Command: curl -f http://localhost:4000/health/liveliness || exit 1
Interval: 30s
Timeout: 10s
Retries: 3
StartPeriod: 60s
```

#### ECS Cluster

| 항목 | 값 |
|-----|-----|
| **이름** | `{stack}-cluster` |
| **Capacity Provider** | FARGATE |

#### ECS Service

| 항목 | 값 |
|-----|-----|
| **이름** | `{stack}-service` |
| **Cluster** | `{stack}-cluster` |
| **Task Definition** | `{stack}-task:N` |
| **Desired Count** | 1 (기본) / `--desired-count`로 변경 |
| **Launch Type** | FARGATE |
| **Network Configuration** | awsvpc, public subnet, `{stack}-task-sg` |
| **AssignPublicIp** | ENABLED |
| **Target Group** | `{stack}-tg` |
| **Container Port** | 4000 |
| **Health Check Grace Period** | 120초 |

---

## 네이밍 컨벤션

모든 리소스는 `{stack_name}` 프리픽스를 사용합니다. 예: `litellm`

```
ALB:             litellm-alb
Target Group:    litellm-tg
Security Groups: litellm-alb-sg, litellm-task-sg, litellm-db-sg
RDS Instance:    litellm-db
DB Subnet Group: litellm-db-subnets
Log Group:       /ecs/litellm-task
IAM Roles:       litellm-ecs-exec-role, litellm-ecs-task-role
ECS Cluster:     litellm-cluster
ECS Service:     litellm-service
ECS Task Def:    litellm-task
Secrets:         litellm/master-key, litellm/db-password
```

---

## 태깅 정책

모든 생성된 리소스에 두 개 태그 추가:

```
Key: Stack        Value: {stack_name}
Key: ManagedBy    Value: litellm-installer
```

(Secrets Manager는 `Stack`만 붙음)

---

## 재배포 및 업데이트

- **같은 리전·스택명으로 재배포**: 기존 리소스 유지, ECS Task Definition만 업데이트
- **Desired Count 변경**: ALB / Target Group은 그대로, ECS Service 스케일만 변경
- **CPU/Memory 변경**: 새 Task Definition 버전 생성, 자동 rolling update

---

## 리소스 삭제 순서

`install/uninstaller.py` 또는 `destroy` 커맨드가 안전하게 제거:

1. ECS Service 삭제
2. ECS Cluster 삭제
3. ALB / Target Group / Listener 삭제
4. RDS 인스턴스 삭제 (5–10분 소요)
5. RDS DB Subnet Group 삭제
6. IAM 역할·정책 삭제
7. Security Groups 삭제
8. Secrets Manager 시크릿 삭제
9. CloudWatch Log Group 삭제

---

## 기본값 및 변경 옵션

```bash
python install/installer.py deploy \
  --region us-west-2                      # AWS 리전 (필수)
  --stack-name litellm                    # 스택 이름 (필수)
  --cpu 1024                              # ECS CPU (기본: 1024)
  --memory 2048                           # ECS Memory MB (기본: 2048)
  --desired-count 1                       # ECS 원하는 작업 수 (기본: 1)
  --db-instance-class db.t3.micro         # RDS 인스턴스 클래스 (기본)
  --db-allocated-storage 20               # RDS 스토리지 GB (기본: 20)
  --log-retention-days 7                  # CloudWatch 보존 일 (기본: 7)
```

---

## 통신 흐름

```
클라이언트 (Claude Code / curl / SDK)
    ↓ (포트 80)
ALB (litellm-alb)
    ↓ (포트 4000, VPC 내부)
ECS Task (LiteLLM Proxy, litellm 컨테이너)
    ↓ (API 호출)
    ├─ RDS PostgreSQL (포트 5432) — DATABASE_URL
    ├─ Secrets Manager — LITELLM_MASTER_KEY
    ├─ CloudWatch Logs — 로깅
    └─ Bedrock / Anthropic / OpenAI 등 — 모델 추론
```

---

## 비용 추정 (us-west-2, 월간)

현재 installer **기본 스펙**(Fargate 1×1vCPU/2GB, ALB 1, RDS `db.t3.micro` 20GB, Secrets 2, public IP, **NAT 없음**) 기준:

| 리소스 | SKU / 산식 | 추정 비용 |
|--------|------------|---------|
| ECS Fargate | 1 vCPU + 2 GB × 730h | ~$36 |
| Public IPv4 | task ENI $0.005/h | ~$4 |
| ALB | 시간요금 + 저트래픽 LCU | ~$20–25 |
| RDS PostgreSQL | db.t3.micro + 20GB | ~$15–18 |
| Secrets Manager | 2 secrets | ~$0.80 |
| CloudWatch Logs | 소량 | ~$1–3 |
| Data Transfer | 소량 | ~$1–5 |
| **인프라 합계** | | **~$80–95 / 월** (전형적 ~$85) |

> Bedrock/Mantle **토큰 요금은 미포함**. README [비용 검토](README.md#비용-검토) 참고.  
> NAT Gateway·Multi-AZ·task 스케일 아웃 시 상단(~$140+)으로 갈 수 있습니다.
