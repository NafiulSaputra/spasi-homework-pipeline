variable "project_id" {
  description = "GCP Project ID"
  type        = string
}

variable "region" {
  description = "Region GCP (default: Jakarta)"
  type        = string
  default     = "asia-southeast2"
}

variable "environment" {
  description = "staging / production"
  type        = string
  default     = "staging"
}

variable "db_tier" {
  description = "Ukuran instance Cloud SQL (db-f1-micro cukup untuk MVP)"
  type        = string
  default     = "db-f1-micro"
}

variable "image_tag" {
  description = "Tag image Docker yang dideploy (CI mengisi dengan commit SHA supaya setiap deploy membuat revisi baru)"
  type        = string
  default     = "latest"
}

variable "google_oauth_client_id" {
  description = "OAuth Client ID untuk login guru via Google"
  type        = string
  default     = ""
}

variable "spreadsheet_id" {
  description = "ID Google Spreadsheet Master Soal (kosongkan untuk menonaktifkan sync Sheets)"
  type        = string
  default     = ""
}

# ---------------- secrets ----------------

variable "db_password" {
  type      = string
  sensitive = true
}

variable "gemini_api_key" {
  type      = string
  sensitive = true
}

variable "whatsapp_api_key" {
  type      = string
  sensitive = true
}

variable "phone_encryption_key" {
  description = "Kunci enkripsi nomor HP (reversible, bukan hash)"
  type        = string
  sensitive   = true
}

variable "nis_hash_pepper" {
  description = "Pepper rahasia untuk hash NIS"
  type        = string
  sensitive   = true
}

variable "jwt_secret" {
  description = "Secret penandatangan token login (JWT)"
  type        = string
  sensitive   = true
}
