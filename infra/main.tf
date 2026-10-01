locals {
  suffix     = var.environment
  zone       = "${var.region}-a"
  repo_url   = "${var.region}-docker.pkg.dev/${var.project_id}/spasi-images-${var.environment}"
  api_image  = "${local.repo_url}/api:${var.image_tag}"
  work_image = "${local.repo_url}/worker:${var.image_tag}"

  # Nama secret (TIDAK sensitif) -> dipakai for_each.
  # Nilainya (sensitif) diambil dari map terpisah di bawah.
  secret_names = toset([
    "db-password", "gemini-api-key", "whatsapp-api-key",
    "phone-encryption-key", "nis-hash-pepper", "jwt-secret",
  ])

  secret_values = {
    "db-password"          = var.db_password
    "gemini-api-key"       = var.gemini_api_key
    "whatsapp-api-key"     = var.whatsapp_api_key
    "phone-encryption-key" = var.phone_encryption_key
    "nis-hash-pepper"      = var.nis_hash_pepper
    "jwt-secret"           = var.jwt_secret
  }

  # Secret -> nama environment variable di aplikasi
  secret_env = {
    "db-password"          = "DB_PASS"
    "gemini-api-key"       = "GEMINI_API_KEY"
    "whatsapp-api-key"     = "WHATSAPP_API_KEY"
    "phone-encryption-key" = "PHONE_ENCRYPTION_KEY"
    "nis-hash-pepper"      = "NIS_HASH_PEPPER"
    "jwt-secret"           = "JWT_SECRET"
  }
}

# ============================================================
# 0. API GCP yang dibutuhkan
# ============================================================
resource "google_project_service" "apis" {
  for_each = toset([
    "run.googleapis.com",
    "sqladmin.googleapis.com",
    "secretmanager.googleapis.com",
    "artifactregistry.googleapis.com",
    "compute.googleapis.com",
    "redis.googleapis.com",
    "vpcaccess.googleapis.com",
    "sheets.googleapis.com",
    "iam.googleapis.com",
  ])
  service            = each.value
  disable_on_destroy = false
}

data "google_compute_network" "default" {
  name       = "default"
  depends_on = [google_project_service.apis]
}

# ============================================================
# 1. ARTIFACT REGISTRY — dibuat duluan oleh CI (terraform apply -target)
#    sebelum image di-build, supaya deploy pertama tidak gagal
#    karena image belum ada (masalah chicken-and-egg).
# ============================================================
resource "google_artifact_registry_repository" "spasi_images" {
  location      = var.region
  repository_id = "spasi-images-${local.suffix}"
  format        = "DOCKER"
  depends_on    = [google_project_service.apis]
}

# ============================================================
# 2. DATABASE — Cloud SQL PostgreSQL
#    Tidak ada authorized network publik. Cloud Run masuk lewat
#    konektor bawaan, worker VM lewat Cloud SQL Auth Proxy (IAM).
# ============================================================
resource "google_sql_database_instance" "spasi_db" {
  name             = "spasi-db-${local.suffix}"
  database_version = "POSTGRES_15"
  region           = var.region

  settings {
    tier = var.db_tier

    backup_configuration {
      enabled    = true
      start_time = "02:00" # backup harian — nilai siswa tidak boleh hilang
    }

    ip_configuration {
      ipv4_enabled = true
      ssl_mode     = "ENCRYPTED_ONLY"
    }
  }

  deletion_protection = var.environment == "production"
  depends_on          = [google_project_service.apis]
}

resource "google_sql_database" "spasi_db_name" {
  name     = "spasi_db"
  instance = google_sql_database_instance.spasi_db.name
}

resource "google_sql_user" "spasi_db_user" {
  name     = "spasi_app"
  instance = google_sql_database_instance.spasi_db.name
  password = var.db_password
}

# ============================================================
# 3. REDIS — Memorystore (antrean tugas_masuk & DLQ)
# ============================================================
resource "google_redis_instance" "queue" {
  name               = "spasi-queue-${local.suffix}"
  tier               = "BASIC"
  memory_size_gb     = 1
  region             = var.region
  authorized_network = data.google_compute_network.default.id
  redis_version      = "REDIS_7_0"
  depends_on         = [google_project_service.apis]
}

# Cloud Run tidak berada di VPC; konektor ini yang membuatnya bisa
# menjangkau IP privat Memorystore.
resource "google_vpc_access_connector" "connector" {
  name          = "spasi-conn-${local.suffix}"
  region        = var.region
  network       = data.google_compute_network.default.name
  ip_cidr_range = "10.8.0.0/28"
  min_instances = 2
  max_instances = 3
  machine_type  = "e2-micro"
  depends_on    = [google_project_service.apis]
}

# ============================================================
# 4. OBJECT STORAGE — file tugas siswa (tanpa akses publik)
# ============================================================
resource "google_storage_bucket" "bucket_tugas" {
  name                        = "${var.project_id}-bucket-tugas-${local.suffix}"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = var.environment != "production"

  lifecycle_rule {
    condition {
      age = 400 # ~1 tahun ajaran + buffer
    }
    action {
      type = "Delete"
    }
  }
}

# ============================================================
# 5. SERVICE ACCOUNT aplikasi (dipakai Cloud Run API & worker VM)
# ============================================================
resource "google_service_account" "app_sa" {
  account_id   = "spasi-app-${local.suffix}"
  display_name = "SPASI application service account"
  depends_on   = [google_project_service.apis]
}

resource "google_storage_bucket_iam_member" "app_bucket" {
  bucket = google_storage_bucket.bucket_tugas.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.app_sa.email}"
}

resource "google_project_iam_member" "app_roles" {
  for_each = toset([
    "roles/cloudsql.client",
    "roles/logging.logWriter",
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.app_sa.email}"
}

resource "google_artifact_registry_repository_iam_member" "app_pull" {
  location   = google_artifact_registry_repository.spasi_images.location
  repository = google_artifact_registry_repository.spasi_images.name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.app_sa.email}"
}

# ============================================================
# 6. SECRET MANAGER — semua kunci rahasia
# ============================================================
resource "google_secret_manager_secret" "secrets" {
  for_each  = local.secret_names
  secret_id = "spasi-${each.key}-${local.suffix}"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "secrets" {
  for_each    = local.secret_names
  secret      = google_secret_manager_secret.secrets[each.key].id
  secret_data = local.secret_values[each.key]
}

resource "google_secret_manager_secret_iam_member" "app_reads" {
  for_each  = local.secret_names
  secret_id = google_secret_manager_secret.secrets[each.key].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.app_sa.email}"
}

# ============================================================
# 7. CLOUD RUN — API
# ============================================================
resource "google_cloud_run_v2_service" "api" {
  name     = "spasi-api-${local.suffix}"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.app_sa.email

    vpc_access {
      connector = google_vpc_access_connector.connector.id
      egress    = "PRIVATE_RANGES_ONLY" # hanya trafik ke Redis lewat VPC
    }

    volumes {
      name = "cloudsql"
      cloud_sql_instance {
        instances = [google_sql_database_instance.spasi_db.connection_name]
      }
    }

    containers {
      image = local.api_image

      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }

      env {
        name  = "DB_HOST"
        value = "/cloudsql/${google_sql_database_instance.spasi_db.connection_name}"
      }
      env {
        name  = "DB_PORT"
        value = "5432"
      }
      env {
        name  = "DB_NAME"
        value = google_sql_database.spasi_db_name.name
      }
      env {
        name  = "DB_USER"
        value = google_sql_user.spasi_db_user.name
      }
      env {
        name  = "REDIS_HOST"
        value = google_redis_instance.queue.host
      }
      env {
        name  = "REDIS_PORT"
        value = tostring(google_redis_instance.queue.port)
      }
      env {
        name  = "GCS_BUCKET_NAME"
        value = google_storage_bucket.bucket_tugas.name
      }
      env {
        name  = "GOOGLE_OAUTH_CLIENT_ID"
        value = var.google_oauth_client_id
      }

      dynamic "env" {
        for_each = local.secret_env
        content {
          name = env.value
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.secrets[env.key].secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }

  depends_on = [
    google_secret_manager_secret_version.secrets,
    google_secret_manager_secret_iam_member.app_reads,
    google_project_iam_member.app_roles,
  ]
}

# API publik (frontend memanggilnya langsung); autentikasi dilakukan
# di level aplikasi (JWT siswa/guru).
resource "google_cloud_run_v2_service_iam_member" "public_invoker" {
  name     = google_cloud_run_v2_service.api.name
  location = google_cloud_run_v2_service.api.location
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# ============================================================
# 8. COMPUTE ENGINE — Worker + Scheduler + Cloud SQL Auth Proxy
#    Debian (sudah ada gcloud CLI bawaan), bukan COS yang tidak punya gcloud.
# ============================================================
resource "google_compute_instance" "worker_vm" {
  name         = "spasi-worker-${local.suffix}"
  machine_type = "e2-small"
  zone         = local.zone

  boot_disk {
    initialize_params {
      image = "debian-cloud/debian-12"
      size  = 20
    }
  }

  network_interface {
    network = data.google_compute_network.default.name
    access_config {} # IP keluar untuk memanggil Gemini & WhatsApp API
  }

  service_account {
    email = google_service_account.app_sa.email
    scopes = [
      "https://www.googleapis.com/auth/cloud-platform",
      "https://www.googleapis.com/auth/spreadsheets",
    ]
  }

  metadata = {
    startup-script = templatefile("${path.module}/worker_startup.sh.tpl", {
      region           = var.region
      worker_image     = local.work_image
      sql_connection   = google_sql_database_instance.spasi_db.connection_name
      db_name          = google_sql_database.spasi_db_name.name
      db_user          = google_sql_user.spasi_db_user.name
      redis_host       = google_redis_instance.queue.host
      redis_port       = google_redis_instance.queue.port
      bucket_name      = google_storage_bucket.bucket_tugas.name
      spreadsheet_id   = var.spreadsheet_id
      secret_env_lines = join("\n", [
        for k, envname in local.secret_env :
        "echo \"${envname}=$(gcloud secrets versions access latest --secret=${google_secret_manager_secret.secrets[k].secret_id})\" >> \"$ENV_FILE\""
      ])
    })
  }

  allow_stopping_for_update = true

  depends_on = [
    google_secret_manager_secret_version.secrets,
    google_secret_manager_secret_iam_member.app_reads,
    google_artifact_registry_repository_iam_member.app_pull,
  ]
}
