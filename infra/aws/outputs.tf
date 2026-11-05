output "cluster_name" {
  description = "EKS cluster name: aws eks update-kubeconfig --name <this>."
  value       = module.eks.cluster_name
}

output "db_host" {
  description = "Value for database.host in the Helm chart."
  value       = aws_db_instance.postgres.address
}

output "db_secret_arn" {
  description = "Secrets Manager secret that holds the master password (JSON with username and password)."
  value       = aws_db_instance.postgres.master_user_secret[0].secret_arn
}

output "redis_url" {
  description = "Value for redis.externalUrl in the Helm chart (TLS, hence rediss://)."
  value       = "rediss://${aws_elasticache_replication_group.redis.primary_endpoint_address}:6379/0"
}
