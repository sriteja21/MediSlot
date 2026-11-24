# MediSlot

MediSlot is an AWS-hosted doctor portal and appointment platform. The checked-in `src/init_lambda/schema.sql` and `infrastructure/cloudformation/data-stack.yml` are the authoritative data contracts for the current implementation.

## Runtime

- `src/auth` handles registration, login, refresh, and logout.
- `src/authorizer` validates access tokens and enforces patient/doctor routes.
- `src/doctor` provides doctor profile, consultation type mutation, availability, blackout, dashboard, and appointment cancellation APIs.
- The doctor portal provides separate Payments and Refunds pages backed by doctor-scoped ledger endpoints; records are ledger state only, since MediSlot does not process payments or transfer refunds.
- `src/patient` provides doctor search, weekly slot booking, appointment/payment/refund-ledger views, patient cancellation, and waitlist management APIs.
- `src/read_api` provides the shared authenticated `GET /slots` endpoint for doctor schedule (`date`) and patient availability (`doctor_id`, `date`, optional `consultation_type_id`) requests, plus the doctor consultation-type list and patient doctor-profile reads. The dedicated Lambda selects role-specific responses using authorizer context; reusable slot storage and seat rules live in `src/read_api/service.py`.
- Patient and doctor cancellations share the refund-tier calculator and slot seat transitions. Refunds are ledger entries only; payments are marked `INITIATED` with `PAY_AT_CLINIC` because MediSlot does not integrate a payment gateway. Cancellation of a booked seat can issue an exclusive waitlist offer for 15 minutes; an EventBridge schedule expires offers and promotes the next waiter.
- `src/shared/notifications.py` persists notification records and sends email through SES. Configure the application stack's `SESFromEmail` parameter with a verified SES sender address; without it, notification records are retained and email delivery is marked blocked.
- `src/slot_materialiser` reconciles the next 30 local calendar days in `MediSlot-Slots-*` after schedule changes and from a daily EC2 systemd timer; it does not use EventBridge. Blackout and obsolete availability slots remain visible with `is_active=false`, and reconciliation never modifies slots with booked seats.
- `src/ec2_app` is the Flask portal served directly by EC2 Nginx over HTTPS, with Gunicorn bound to localhost. Shared browser time and INR formatting helpers are in `src/ec2_app/static/js/shared.js`. A stable Elastic IP is assigned by the EC2 portal stack.
- RDS and DynamoDB key/index definitions are in `infrastructure/cloudformation/data-stack.yml`. Slot IDs are deterministic `<consultation_type_id>#<start_epoch>` values. The Slots table uses `doctor_id` and `slot_sk` (`<start_epoch>#<consultation_type_id>`) as its primary key, `gsi_type_time` for single-slot lookup, and `gsi_doctor_time` (`doctor_id`, `start_time`) for schedule queries.

## Deployment

The GitHub Actions workflow deploys Network, Data, IAM, initializes the database schema and sample registration catalogs, then deploys the SAM application and EC2 portal. A push deploys to `dev`; `workflow_dispatch` can select `dev`, `test`, or `prod`. The sample hospitals are fictional; replace them with the real provider catalog before production use.

The parameter JSON bundles and `infrastructure/scripts/cloudformation-parameters.sh` are restored under `infrastructure/cloudformation/parameters/` and `infrastructure/scripts/`. They are not wired into the current deployment workflow, which uses inline overrides; reconcile their parameter keys with the current templates before using them for deployment.

Configure these GitHub environment secrets:

- `AWS_DEPLOYMENT_ROLE_ARN`: role assumed by the workflow. It needs CloudFormation deployment permissions, permission to write/delete the database SecureString parameter, and permission to upload SAM Lambda and EC2 artifacts to the environment's reports bucket.
- `MEDISLOT_DB_MASTER_PASSWORD`: password for the RDS master account.
- `AWS_REGION`: optional; defaults to `ap-south-1`.

Before deploying the application, provision SecureString parameters `/MediSlot/{environment}/auth/jwt-secret` and `/MediSlot/{environment}/database/application-credentials` in Systems Manager Parameter Store. The application stack creates the environment-specific refund policy and waitlist hold-duration parameters. The database credentials value must be JSON with `username` and `password`; CI writes the RDS master password to `/MediSlot/{environment}/database/master-password-secure` as a SecureString and removes the legacy plaintext parameter. The EC2 website does not require app-specific SSM parameters: bootstrap generates its Flask session key and a self-signed TLS certificate locally.

For a first deployment, deploy the Network stack first and read its `PortalElasticIp` output:

```powershell
aws cloudformation deploy --template-file infrastructure/cloudformation/network-stack.yml --stack-name MediSlot-Network-dev --parameter-overrides Environment=dev
$portalIp = aws cloudformation describe-stacks --stack-name MediSlot-Network-dev --query "Stacks[0].Outputs[?OutputKey=='PortalElasticIp'].OutputValue | [0]" --output text
```

No additional SSM setup is needed for the portal. Bootstrap creates a self-signed certificate containing `$portalIp` as an IP SAN and a random Flask secret stored locally with mode `0600`. Browsers will show a certificate trust warning; use the browser's advanced option to continue for development. For a trusted, warning-free public site, later replace this with a certificate from a trusted issuer.

The portal accepts public HTTPS on TCP 443 only. It does not expose SSH or the Gunicorn port. Manage the instance with AWS Systems Manager Session Manager (`aws ssm start-session --target INSTANCE_ID --region AWS_REGION`); the instance role includes `AmazonSSMManagedInstanceCore`. The stack does not create Route 53 records.

The EC2 stack downloads a versioned ZIP from the private reports bucket during instance bootstrap. CloudFormation waits up to 30 minutes for Nginx, Gunicorn, and `/healthz` to pass before marking the instance created; bootstrap output is written to `/var/log/medislot-bootstrap.log` and the EC2 console. Its health endpoint is `/healthz`; the stack output and deployment log include the HTTPS portal URL.

## Python dependencies

```text
PyJWT==2.10.1
bcrypt==4.2.1
PyMySQL==1.1.1
boto3>=1.35.0,<2.0.0
pytest>=8.0.0,<9.0.0
```