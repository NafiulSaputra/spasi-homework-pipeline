terraform {
  required_version = ">= 1.5.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }

  # Remote state di GCS — WAJIB untuk CI/CD. Tanpa ini, setiap runner
  # GitHub Actions mulai dari state kosong dan mencoba membuat ulang
  # semua resource (error "already exists" di deploy kedua).
  # Bucket diisi saat init:
  #   terraform init -backend-config="bucket=NAMA_BUCKET" -backend-config="prefix=spasi/staging"
  # Buat bucket-nya sekali lewat: scripts/bootstrap_tf_state.sh
  backend "gcs" {}
}

provider "google" {
  project = var.project_id
  region  = var.region
}
