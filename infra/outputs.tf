output "api_url" {
  description = "URL API di Cloud Run — isi ke frontend: index.html?api=<URL>"
  value       = google_cloud_run_v2_service.api.uri
}

output "app_service_account_email" {
  description = "Share Google Spreadsheet guru ke email ini sebagai Editor (untuk sync Sheets)"
  value       = google_service_account.app_sa.email
}

output "db_connection_name" {
  value = google_sql_database_instance.spasi_db.connection_name
}

output "bucket_tugas_name" {
  value = google_storage_bucket.bucket_tugas.name
}

output "redis_host" {
  value = google_redis_instance.queue.host
}

output "artifact_registry_repo" {
  value = local.repo_url
}

output "worker_vm_restart_command" {
  description = "Jalankan setelah push image worker baru (CI melakukannya otomatis)"
  value       = "gcloud compute instances reset ${google_compute_instance.worker_vm.name} --zone=${local.zone}"
}
