# Rush Hour on AWS (Terraform, validate-only)

Describes where the chart would run: VPC (3 AZs, one NAT), EKS with one managed node group (3-6 x `m6i.large`),
RDS Postgres 16 (Multi-AZ, encrypted, password in Secrets Manager) and ElastiCache Valkey (2 nodes, TLS).
Only the EKS nodes can reach RDS and Redis. Images stay on GHCR, so there is no ECR.

**Validate-only on purpose:** CI runs `terraform fmt`, `validate` and `trivy config`. Nothing is applied, no state, no credentials.
Rough cost if it were applied (estimate, eu-central-1, on-demand): about 450 USD/month (nodes 3 x ~100, EKS ~75, NAT ~40, RDS Multi-AZ ~150, Valkey 2 x ~25).

Plug the outputs into the chart (the Secrets Manager secret must be synced into a Secret with the keys
`password` and `userlist.txt`, e.g. with External Secrets, and named in `database.existingSecret`):

```sh
helm upgrade --install rush-hour deploy/helm/rush-hour \
  --set postgres.enabled=false --set database.host="$(terraform output -raw db_host)" \
  --set redis.enabled=false    --set redis.externalUrl="$(terraform output -raw redis_url)" \
  --set database.existingSecret=rush-hour-db
```
