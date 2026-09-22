#!/usr/bin/env bash
# Ejecutar desde CloudShell, NO desde la instancia que será eliminada.
set -euo pipefail
[[ -f resources.env ]] || { echo 'Falta resources.env'; exit 1; }
source ./resources.env
: "${AWS_REGION:?}" "${S3_BUCKET:?}" "${EC2_INSTANCE_ID:?}" "${RDS_INSTANCE_ID:?}" "${RDS_SECRET_ID:?}" "${EC2_SG_ID:?}" "${RDS_SG_ID:?}"
export AWS_DEFAULT_REGION="$AWS_REGION" AWS_PAGER=""
[[ "$EC2_INSTANCE_ID" == i-* && "$EC2_SG_ID" == sg-* && "$RDS_SG_ID" == sg-* ]] || exit 1
aws sts get-caller-identity --query '{Account:Account,Arn:Arn}'
echo "Se eliminarán: $EC2_INSTANCE_ID $RDS_INSTANCE_ID $S3_BUCKET $RDS_SECRET_ID $EC2_SG_ID $RDS_SG_ID ${DB_SUBNET_GROUP:-}"
echo 'Sin snapshot final: se perderán definitivamente las fotos y datos de esta actividad.'
read -r -p 'Escribe ELIMINAR para continuar: ' confirm
[[ "$confirm" == ELIMINAR ]] || exit 1
# Este proyecto exige bucket sin versionado para borrar físicamente los originales.
versioning=$(aws s3api get-bucket-versioning --bucket "$S3_BUCKET" --query Status --output text)
[[ "$versioning" == None ]] || { echo 'Bucket versionado: revisa y elimina también sus versiones; no se continúa.'; exit 1; }
aws ec2 terminate-instances --instance-ids "$EC2_INSTANCE_ID" --output json
aws rds delete-db-instance --db-instance-identifier "$RDS_INSTANCE_ID" --skip-final-snapshot --delete-automated-backups --output json
aws s3 rm "s3://$S3_BUCKET" --recursive
aws s3api delete-bucket --bucket "$S3_BUCKET"
aws secretsmanager delete-secret --secret-id "$RDS_SECRET_ID" --force-delete-without-recovery
aws ec2 wait instance-terminated --instance-ids "$EC2_INSTANCE_ID"
# RDS puede tardar más que el límite del waiter. En ese caso continuar manualmente
# con las instrucciones del README; no se anuncia éxito prematuramente.
aws rds wait db-instance-deleted --db-instance-identifier "$RDS_INSTANCE_ID"
aws ec2 delete-security-group --group-id "$RDS_SG_ID"
aws ec2 delete-security-group --group-id "$EC2_SG_ID"
if [[ -n "${DB_SUBNET_GROUP:-}" ]]; then
    aws rds delete-db-subnet-group --db-subnet-group-name "$DB_SUBNET_GROUP"
fi
# Verificar códigos esperados; un AccessDenied NO cuenta como recurso eliminado.
assert_absent() {
    local expected="$1"; shift
    local result
    if result=$("$@" 2>&1); then
        echo "ERROR: el recurso aún existe: $*"; exit 1
    elif [[ "$result" != *"$expected"* ]]; then
        echo "$result"; exit 1
    fi
    echo "Ausencia confirmada: $expected"
}
assert_absent DBInstanceNotFound aws rds describe-db-instances --db-instance-identifier "$RDS_INSTANCE_ID"
assert_absent NoSuchBucket aws s3api list-objects-v2 --bucket "$S3_BUCKET" --max-keys 1
assert_absent InvalidGroup.NotFound aws ec2 describe-security-groups --group-ids "$RDS_SG_ID"
assert_absent InvalidGroup.NotFound aws ec2 describe-security-groups --group-ids "$EC2_SG_ID"
# La eliminación del secret es asíncrona: esperar ausencia real, máximo 5 minutos.
secret_absent=false
for attempt in {1..60}; do
    if result=$(aws secretsmanager describe-secret --secret-id "$RDS_SECRET_ID" --query DeletedDate --output text 2>&1); then
        sleep 5
    elif [[ "$result" == *ResourceNotFoundException* ]]; then
        secret_absent=true
        break
    else
        echo "$result"; exit 1
    fi
done
[[ "$secret_absent" == true ]] || { echo 'Secret aún en eliminación; comprueba más tarde. Limpieza pendiente de confirmar.'; exit 1; }
echo 'Secret ausente: ResourceNotFoundException.'
aws ec2 describe-instances --instance-ids "$EC2_INSTANCE_ID" --query 'Reservations[].Instances[].State.Name'
echo 'Limpieza verificada. EC2 puede seguir visible temporalmente como terminated.'
