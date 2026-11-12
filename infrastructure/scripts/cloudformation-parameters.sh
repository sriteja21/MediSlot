resolve_cfn_parameters() {
  local parameter_file="$1"
  local environment="$2"
  local db_master_password="${3-}"
  local app_artifact_key="${4-}"
  local resolved_parameters

  if [[ ! -f "$parameter_file" ]]; then
    echo "::error::CloudFormation parameter file not found: $parameter_file"
    return 1
  fi

  resolved_parameters="$(
    jq -er \
      --arg environment "$environment" \
      --arg db_master_password "$db_master_password" \
      --arg app_artifact_key "$app_artifact_key" '
        if type != "array" then
          error("parameter file must contain a JSON array")
        elif any(.[]; .ParameterKey == "DBMasterPassword") and $db_master_password == "" then
          error("DB master password must be supplied from the GitHub secret")
        elif any(.[]; .ParameterKey == "AppArtifactKey") and $app_artifact_key == "" then
          error("application artifact key must be supplied")
        else
          map(
            if .ParameterKey == "DBMasterPassword" then
              .ParameterValue = $db_master_password
            elif .ParameterKey == "AppArtifactKey" then
              .ParameterValue = $app_artifact_key
            else
              .ParameterValue |= gsub("__ENVIRONMENT__"; $environment)
            end
          )
          | .[]
          | "\(.ParameterKey)=\(.ParameterValue)"
        end
      ' "$parameter_file"
  )" || return 1

  mapfile -t CFN_PARAMETER_OVERRIDES <<< "$resolved_parameters"
  if [[ "${#CFN_PARAMETER_OVERRIDES[@]}" -eq 0 ]]; then
    echo "::error::CloudFormation parameter file is empty: $parameter_file"
    return 1
  fi
}
