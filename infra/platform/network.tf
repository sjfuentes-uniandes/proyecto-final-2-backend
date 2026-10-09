resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = local.prefix }
}

# Solo alojan los NAT Gateway: ninguna tarea recibe IP pública.
resource "aws_subnet" "public" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index)
  availability_zone = local.azs[count.index]
  tags              = { Name = "${local.prefix}-public-${local.azs[count.index]}", Tier = "public" }
}

# Tareas ECS, NLB privado y endpoints, distribuidos en las zonas A y B.
resource "aws_subnet" "private" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, 10 + count.index)
  availability_zone = local.azs[count.index]
  tags              = { Name = "${local.prefix}-private-${local.azs[count.index]}", Tier = "private" }
}

# Sin ruta a Internet.
resource "aws_subnet" "database" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, 20 + count.index)
  availability_zone = local.azs[count.index]
  tags              = { Name = "${local.prefix}-database-${local.azs[count.index]}", Tier = "database" }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = local.prefix }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }
  tags = { Name = "${local.prefix}-public" }
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

locals {
  # NAT Gateway: uno compartido, o uno por zona con alta disponibilidad.
  nat_gateway_count = var.egress_mode == "nat_gateway" ? (var.high_availability ? 2 : 1) : 0
}

resource "aws_eip" "nat" {
  count  = local.nat_gateway_count
  domain = "vpc"
  tags   = { Name = "${local.prefix}-nat-${count.index}" }
}

resource "aws_nat_gateway" "main" {
  count         = local.nat_gateway_count
  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index].id
  tags          = { Name = "${local.prefix}-${local.azs[count.index]}" }
  depends_on    = [aws_internet_gateway.main]
}

# NAT instance: alternativa de capa gratuita al NAT Gateway. Una sola instancia
# en la zona A; si falla, EC2 la recupera, pero la salida a Internet (aliados y
# APIs de AWS) se interrumpe mientras tanto. Las tareas no tienen IP pública.
data "aws_ssm_parameter" "nat_ami" {
  count = var.egress_mode == "nat_instance" ? 1 : 0
  name  = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

resource "aws_security_group" "nat" {
  count       = var.egress_mode == "nat_instance" ? 1 : 0
  name        = "${local.prefix}-nat"
  description = "NAT instance: trafico saliente de las subredes privadas"
  vpc_id      = aws_vpc.main.id
}

resource "aws_vpc_security_group_ingress_rule" "nat" {
  count             = var.egress_mode == "nat_instance" ? 2 : 0
  security_group_id = aws_security_group.nat[0].id
  cidr_ipv4         = aws_subnet.private[count.index].cidr_block
  ip_protocol       = "-1"
}

resource "aws_vpc_security_group_egress_rule" "nat" {
  count             = var.egress_mode == "nat_instance" ? 1 : 0
  security_group_id = aws_security_group.nat[0].id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

resource "aws_instance" "nat" {
  count                       = var.egress_mode == "nat_instance" ? 1 : 0
  ami                         = data.aws_ssm_parameter.nat_ami[0].value
  instance_type               = var.nat_instance_type
  subnet_id                   = aws_subnet.public[0].id
  vpc_security_group_ids      = [aws_security_group.nat[0].id]
  associate_public_ip_address = true
  source_dest_check           = false
  user_data_replace_on_change = true
  user_data                   = <<-EOT
    #!/bin/bash
    set -euxo pipefail
    dnf install -y iptables-services
    echo 'net.ipv4.ip_forward = 1' > /etc/sysctl.d/90-nat.conf
    sysctl -p /etc/sysctl.d/90-nat.conf
    IFACE=$(ip route show default | awk '{print $5; exit}')
    iptables -t nat -A POSTROUTING -o "$IFACE" -j MASQUERADE
    iptables -P FORWARD ACCEPT
    iptables -F FORWARD
    iptables-save > /etc/sysconfig/iptables
    systemctl enable --now iptables
  EOT
  metadata_options {
    http_tokens = "required"
  }
  root_block_device {
    volume_type = "gp3"
    volume_size = 8
    encrypted   = true
  }
  maintenance_options {
    auto_recovery = "default"
  }
  tags = { Name = "${local.prefix}-nat" }
  lifecycle {
    ignore_changes = [ami]
  }
  depends_on = [aws_internet_gateway.main]
}

resource "aws_route_table" "private" {
  count  = 2
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${local.prefix}-private-${local.azs[count.index]}" }
}

resource "aws_route" "private_egress" {
  count                  = 2
  route_table_id         = aws_route_table.private[count.index].id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = var.egress_mode == "nat_gateway" ? try(aws_nat_gateway.main[min(count.index, local.nat_gateway_count - 1)].id, null) : null
  network_interface_id   = var.egress_mode == "nat_instance" ? try(aws_instance.nat[0].primary_network_interface_id, null) : null
}

resource "aws_route_table_association" "private" {
  count          = 2
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}

resource "aws_route_table" "database" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${local.prefix}-database" }
}

resource "aws_route_table_association" "database" {
  count          = 2
  subnet_id      = aws_subnet.database[count.index].id
  route_table_id = aws_route_table.database.id
}

# Gratuito: capas de ECR, evidencias y auditoría viajan a S3 sin pasar por el NAT.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = aws_route_table.private[*].id
  tags              = { Name = "${local.prefix}-s3" }
}

resource "aws_security_group" "endpoints" {
  count       = length(var.interface_endpoints) > 0 ? 1 : 0
  name        = "${local.prefix}-endpoints"
  description = "HTTPS desde la VPC hacia endpoints de interfaz"
  vpc_id      = aws_vpc.main.id
}

resource "aws_vpc_security_group_ingress_rule" "endpoints" {
  count             = length(var.interface_endpoints) > 0 ? 1 : 0
  security_group_id = aws_security_group.endpoints[0].id
  cidr_ipv4         = var.vpc_cidr
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_endpoint" "interface" {
  for_each            = toset(var.interface_endpoints)
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.aws_region}.${each.key}"
  vpc_endpoint_type   = "Interface"
  private_dns_enabled = true
  subnet_ids          = var.high_availability ? aws_subnet.private[*].id : [aws_subnet.private[0].id]
  security_group_ids  = [aws_security_group.endpoints[0].id]
  tags                = { Name = "${local.prefix}-${each.key}" }
}

resource "aws_security_group" "database" {
  name        = "${local.prefix}-database"
  description = "PostgreSQL; las reglas de entrada por servicio se crean en apps"
  vpc_id      = aws_vpc.main.id
}
