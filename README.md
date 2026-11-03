# MediSlot

MediSlot is an AWS-hosted doctor portal and appointment platform. The checked-in `src/init_lambda/schema.sql` and `infrastructure/cloudformation/data-stack.yml` are the authoritative data contracts for the current implementation.

## Runtime

- `src/auth` handles registration, login, refresh, and logout.
- `src/authorizer` validates access tokens and enforces patient/doctor routes.
- `src/doctor` provides the doctor profile, consultation type, availability, blackout, schedule, and dashboard API.
- `src/slot_materialiser` runs daily and writes deterministic slots to the existing `MediSlot-Slots-*` table.
- `src/ec2_app` is the Flask doctor portal, deployed on EC2 behind an Application Load Balancer and Nginx, with Gunicorn serving Flask.
- RDS and DynamoDB key/index definitions are in `infrastructure/cloudformation/data-stack.yml`. Slot IDs are deterministic UUIDv5 values compatible with `appointments.slot_id CHAR(36)`. Slot writes use `slotId`; doctor schedule reads use `GSI1-DoctorStart` (`doctorId`, `startUtcConsultationType`).

## Deployment

The GitHub Actions workflow deploys Network, Data, IAM, initializes the database schema and sample registration catalogs, then deploys the SAM application and EC2 portal. A push deploys to `dev`; `workflow_dispatch` can select `dev`, `test`, or `prod`. The sample hospitals are fictional; replace them with the real provider catalog before production use.

Configure these GitHub environment secrets:

- `AWS_DEPLOYMENT_ROLE_ARN`: role assumed by the workflow. It needs CloudFormation deployment permissions and access to upload the EC2 artifact to the environment's reports bucket.
- `MEDISLOT_DB_MASTER_PASSWORD`: password for the RDS master account.
- `AWS_REGION`: optional; defaults to `ap-south-1`.

Before deploying the application, provision SecureString parameters `/MediSlot/{environment}/auth/jwt-secret` and `/MediSlot/{environment}/database/application-credentials`. The database credentials value must be JSON with `username` and `password`; the workflow creates the separate master-password parameter used by the initializer.

For `prod`, configure the GitHub environment variables `MEDISLOT_ACM_CERTIFICATE_ARN` and `MEDISLOT_PORTAL_DOMAIN`. The certificate must cover the DNS name, and that name must resolve to the deployed load balancer. Dev/test can run over HTTP when these variables are unset.

The EC2 stack downloads a versioned ZIP from the private reports bucket during instance bootstrap. Its health endpoint is `/healthz`; the stack output and deployment log include the portal URL.

## Python dependencies

```text
PyJWT==2.10.1
bcrypt==4.2.1
PyMySQL==1.1.1
boto3>=1.35.0,<2.0.0
pytest>=8.0.0,<9.0.0
```