terraform {
  required_version = ">= 1.7"

  # No backend on purpose: this setup is validated in CI and never applied.
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.72"
    }
  }
}

provider "aws" {
  region = var.region
}
