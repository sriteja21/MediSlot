# MediSlot

MediSlot is an AWS-hosted doctor portal and appointment platform. The checked-in `src/init_lambda/schema.sql` and `infrastructure/cloudformation/data-stack.yml` are the authoritative data contracts for the current implementation.

## Runtime

- `src/auth` handles registration, login, refresh, and logout.
- `src/authorizer` validates access tokens and enforces patient/doctor routes.
- `src/doctor` provides the doctor profile, consultation type, availability, blackout, schedule, and dashboard API.
- `src/slot_materialiser` writes deterministic slots to the existing `MediSlot-Slots-*` table when doctors change consultation types, availability, or blackouts; it does not use EventBridge.
- `src/ec2_app` is the Flask portal served directly by EC2 Nginx over HTTPS, with Gunicorn bound to localhost. A stable Elastic IP is assigned by the EC2 portal stack.
- RDS and DynamoDB key/index definitions are in `infrastructure/cloudformation/data-stack.yml`. Slot IDs are deterministic UUIDv5 values compatible with `appointments.slot_id CHAR(36)`. Slot writes use `slotId`; doctor schedule reads use `GSI1-DoctorStart` (`doctorId`, `startUtcConsultationType`).

## Deployment

The GitHub Actions workflow deploys Network, Data, IAM, initializes the database schema and sample registration catalogs, then deploys the SAM application and EC2 portal. A push deploys to `dev`; `workflow_dispatch` can select `dev`, `test`, or `prod`. The sample hospitals are fictional; replace them with the real provider catalog before production use.

Configure these GitHub environment secrets:

- `AWS_DEPLOYMENT_ROLE_ARN`: role assumed by the workflow. It needs CloudFormation deployment permissions, permission to write/delete the database SecureString parameter, and access to upload the EC2 artifact to the environment's reports bucket.
- `MEDISLOT_DB_MASTER_PASSWORD`: password for the RDS master account.
- `AWS_REGION`: optional; defaults to `ap-south-1`.

Before deploying the application, provision SecureString parameters `/MediSlot/{environment}/auth/jwt-secret`, `/MediSlot/{environment}/database/application-credentials`, `/MediSlot/{environment}/ec2-app/flask-secret`, `/MediSlot/{environment}/ec2-app/tls-certificate`, and `/MediSlot/{environment}/ec2-app/tls-private-key` in Systems Manager Parameter Store. The database credentials value must be JSON with `username` and `password`; CI writes the RDS master password to `/MediSlot/{environment}/database/master-password-secure` as a SecureString and removes the legacy plaintext parameter. The TLS values must be PEM certificate and private key contents, and the certificate must include the portal IP as an IP SAN. The Flask app reads its signing key at startup with boto3 `ssm:GetParameter(WithDecryption=True)`; the EC2 instance role is scoped to its parameters.

For a first deployment, deploy the Network stack first and read its `PortalElasticIp` output:

```powershell
aws cloudformation deploy --template-file infrastructure/cloudformation/network-stack.yml --stack-name MediSlot-Network-dev --parameter-overrides Environment=dev
$portalIp = aws cloudformation describe-stacks --stack-name MediSlot-Network-dev --query "Stacks[0].Outputs[?OutputKey=='PortalElasticIp'].OutputValue | [0]" --output text
```

Obtain a certificate with an IP SAN matching `$portalIp`, then create the three `/MediSlot/dev/ec2-app/` parameters in the SSM Parameter Store console as `SecureString`: a random Flask secret, the certificate PEM, and its matching private key PEM. Repeat with the environment-specific stack/parameter names for `test` and `prod`. The portal stack does not create DNS records.

The portal accepts public HTTPS on TCP 443 only. It does not expose SSH or the Gunicorn port. Manage the instance with AWS Systems Manager Session Manager (`aws ssm start-session --target INSTANCE_ID --region AWS_REGION`); the instance role includes `AmazonSSMManagedInstanceCore`. The stack does not create Route 53 records. Do not disable TLS for a public login portal; use a certificate whose SAN matches the stable portal address.

The EC2 stack downloads a versioned ZIP from the private reports bucket during instance bootstrap. Its health endpoint is `/healthz`; the stack output and deployment log include the HTTPS portal URL.

## Python dependencies

```text
PyJWT==2.10.1
bcrypt==4.2.1
PyMySQL==1.1.1
boto3>=1.35.0,<2.0.0
pytest>=8.0.0,<9.0.0
```