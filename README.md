# MediSlot

MediSlot is an AWS-hosted doctor portal and appointment platform. The checked-in `src/init_lambda/schema.sql` and `infrastructure/cloudformation/data-stack.yml` are the authoritative data contracts for the current implementation.

## Runtime

- `src/auth` handles registration, login, refresh, and logout.
- `src/authorizer` validates access tokens and enforces patient/doctor routes.
- `src/doctor` provides the doctor profile, consultation type, availability, blackout, schedule, and dashboard API.
- `src/patient` provides patient profile management, public doctor discovery, doctor profiles, and slot availability. Booking and appointment-list workflows are out of scope for Milestone 4.
- `src/slot_materialiser` reconciles the next 30 local calendar days in `MediSlot-Slots-*` after schedule changes and from a daily EC2 systemd timer; it does not use EventBridge. Blackout and obsolete availability slots remain visible with `is_active=false`, and reconciliation never modifies slots with booked seats.
- `src/ec2_app` is the Flask portal served directly by EC2 Nginx over HTTPS, with Gunicorn bound to localhost. A stable Elastic IP is assigned by the EC2 portal stack.
- RDS and DynamoDB key/index definitions are in `infrastructure/cloudformation/data-stack.yml`. Slot IDs are deterministic `<consultation_type_id>#<start_epoch>` values. The Slots table uses `slot_id` as its key and `gsi_doctor_time` (`doctor_id`, `start_time`) for doctor schedules.

## Deployment

The GitHub Actions workflow deploys Network, Data, IAM, initializes the database schema and sample registration catalogs, then deploys the SAM application and EC2 portal. A push deploys to `dev`; `workflow_dispatch` can select `dev`, `test`, or `prod`. The sample hospitals are fictional; replace them with the real provider catalog before production use.

Configure these GitHub environment secrets:

- `AWS_DEPLOYMENT_ROLE_ARN`: role assumed by the workflow. It needs CloudFormation deployment permissions, permission to write/delete the database SecureString parameter, and permission to upload SAM Lambda and EC2 artifacts to the environment's reports bucket.
- `MEDISLOT_DB_MASTER_PASSWORD`: password for the RDS master account.

CloudFormation parameter values for every stack are maintained in `infrastructure/cloudformation/parameters/`. The deployment workflow resolves the selected environment in these files; the RDS master password remains a GitHub secret and is never stored in the repository. The AWS region is configured as `ap-south-1` in the workflow and stack parameter files. Doctor-search index rationale and the SQL needed to capture plans against the deployed RDS schema are in [docs/doctor-search-indexes.md](./docs/doctor-search-indexes.md). Patient deletion behavior, including the Milestone 5 appointment-preservation rule, is documented in [docs/patient-account-deletion.md](./docs/patient-account-deletion.md).

Before deploying the application, provision SecureString parameters `/MediSlot/{environment}/auth/jwt-secret` and `/MediSlot/{environment}/database/application-credentials` in Systems Manager Parameter Store. The database credentials value must be JSON with `username` and `password`; CI writes the RDS master password to `/MediSlot/{environment}/database/master-password-secure` as a SecureString and removes the legacy plaintext parameter. The EC2 website does not require app-specific SSM parameters: bootstrap generates its Flask session key and a self-signed TLS certificate locally.

For a first deployment, deploy the Network stack first and read its `PortalElasticIp` output:

The EC2 stack downloads a versioned ZIP from the private reports bucket during instance bootstrap. CloudFormation waits up to 30 minutes for Nginx, Gunicorn, and `/healthz` to pass before marking the instance created; bootstrap output is written to `/var/log/medislot-bootstrap.log` and the EC2 console. Its health endpoint is `/healthz`; the stack output and deployment log include the HTTPS portal URL.

## Python dependencies

```text
PyJWT==2.10.1
bcrypt==4.2.1
PyMySQL==1.1.1
boto3>=1.35.0,<2.0.0
pytest>=8.0.0,<9.0.0
```