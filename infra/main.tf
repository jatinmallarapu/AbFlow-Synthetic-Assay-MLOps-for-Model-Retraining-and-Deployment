terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

variable "region" { type = string }
variable "image" { type = string }
variable "execution_role_arn" { type = string }
variable "database_secret_arn" { type = string }
variable "private_subnets" { type = list(string) }
variable "security_groups" { type = list(string) }

provider "aws" {
  region = var.region
}

resource "aws_ecs_cluster" "demo" {
  name = "abflow"
}

resource "aws_cloudwatch_log_group" "demo" {
  name              = "/ecs/abflow"
  retention_in_days = 14
}

resource "aws_ecs_task_definition" "api" {
  family                   = "abflow"
  cpu                      = 512
  memory                   = 1024
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  execution_role_arn       = var.execution_role_arn
  container_definitions = jsonencode([{
    name      = "api"
    image     = var.image
    essential = true
    portMappings = [{ containerPort = 8000 }]
    secrets = [{ name = "DATABASE_URL", valueFrom = var.database_secret_arn }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.demo.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "api"
      }
    }
  }])
}

resource "aws_ecs_service" "api" {
  name            = "abflow-api"
  cluster         = aws_ecs_cluster.demo.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = 1
  launch_type     = "FARGATE"
  network_configuration {
    subnets         = var.private_subnets
    security_groups = var.security_groups
  }
}
