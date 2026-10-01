#!/bin/bash
# Jalankan SEKALI sebelum CI pertama: membuat bucket GCS untuk Terraform state.
# Pakai: bash scripts/bootstrap_tf_state.sh <PROJECT_ID> <NAMA_BUCKET> [REGION]
# Lalu simpan NAMA_BUCKET sebagai GitHub Secret: TF_STATE_BUCKET
set -euo pipefail

PROJECT_ID="${1:?PROJECT_ID wajib}"
BUCKET="${2:?NAMA_BUCKET wajib (harus unik global, mis. spasi-tfstate-<project_id>)}"
REGION="${3:-asia-southeast2}"

gcloud storage buckets create "gs://$BUCKET" \
  --project="$PROJECT_ID" --location="$REGION" --uniform-bucket-level-access
# Versioning: state lama bisa dipulihkan kalau ada apply yang salah
gcloud storage buckets update "gs://$BUCKET" --versioning

echo "Bucket state siap: gs://$BUCKET"
echo "Tambahkan GitHub Secret TF_STATE_BUCKET=$BUCKET"
