args <- commandArgs(trailingOnly = TRUE)

get_arg <- function(name) {
  idx <- match(name, args)
  if (is.na(idx) || idx == length(args)) {
    stop(paste("Missing required argument:", name), call. = FALSE)
  }
  args[[idx + 1]]
}

lock_file <- normalizePath(get_arg("--lock-file"), mustWork = TRUE)
project_dir <- normalizePath(get_arg("--project-dir"), mustWork = FALSE)
cache_dir <- normalizePath(get_arg("--cache-dir"), mustWork = FALSE)
output_dir <- normalizePath(get_arg("--output-dir"), mustWork = FALSE)
platform <- get_arg("--platform")

library_dir <- if ("--library-dir" %in% args) normalizePath(get_arg("--library-dir"), mustWork = FALSE) else normalizePath(file.path(output_dir, "library"), mustWork = FALSE)
packages_file <- if ("--packages-file" %in% args) normalizePath(get_arg("--packages-file"), mustWork = TRUE) else NULL
clean_restore <- if ("--clean" %in% args) tolower(get_arg("--clean")) == "true" else FALSE

dir.create(project_dir, recursive = TRUE, showWarnings = FALSE)
dir.create(cache_dir, recursive = TRUE, showWarnings = FALSE)
dir.create(output_dir, recursive = TRUE, showWarnings = FALSE)
dir.create(library_dir, recursive = TRUE, showWarnings = FALSE)
file.copy(lock_file, file.path(project_dir, "renv.lock"), overwrite = TRUE)

log_path <- file.path(output_dir, "restore.log")
log_con <- file(log_path, open = "wt")
sink(log_con, type = "output")
sink(log_con, type = "message")
on.exit({
  while (sink.number(type = "message") > 0) sink(type = "message")
  while (sink.number() > 0) sink()
  close(log_con)
}, add = TRUE)

# Prefer CRAN but fall back to Posit Public Package Manager to reduce “not available” mirror glitches.
options(repos = c(
  RSPM = "https://packagemanager.posit.co/all/latest",
  CRAN = "https://cloud.r-project.org"
))
Sys.setenv(
  RENV_PATHS_CACHE = cache_dir,
  RENV_CONFIG_CACHE_SYMLINKS = "FALSE",
  RENV_CONFIG_PAK_ENABLED = "FALSE",
  RENV_CONFIG_EXTERNAL_LIBRARIES = "/opt/R/4.4.0/lib64/R/library"
)
# Symlink system library into the project library so prebuilt packages are directly reused.
dir.create(library_dir, recursive = TRUE, showWarnings = FALSE)
system_lib <- "/opt/R/4.4.0/lib64/R/library"
file.symlink(list.files(system_lib, full.names = TRUE), file.path(library_dir, basename(list.files(system_lib, full.names = TRUE))))
.libPaths(unique(c(library_dir, system_lib, .libPaths())))

if (!requireNamespace("renv", quietly = TRUE)) {
  install.packages("renv")
}
# Pre-fetch common problematic deps to avoid "not available" failures during targeted restores.
# Generic retry helper for any package set; used later if needed.
install_with_retry <- function(pkgs, attempts = 3) {
  for (i in seq_len(attempts)) {
    ok <- tryCatch({
      install.packages(pkgs, dependencies = TRUE)
      TRUE
    }, error = function(e) FALSE, warning = function(w) TRUE)
    if (ok) break
  }
}

write_root_cause <- function(summary, details = character()) {
  path <- file.path(output_dir, "restore-root-cause.txt")
  lines <- c(summary, details)
  writeLines(lines, path)
}

renv::consent(provided = TRUE)
setwd(project_dir)

packages_to_restore <- NULL
if (!is.null(packages_file)) {
  packages_to_restore <- trimws(readLines(packages_file, warn = FALSE))
  packages_to_restore <- packages_to_restore[nzchar(packages_to_restore)]
  if (!length(packages_to_restore)) {
    packages_to_restore <- NULL
  }
}

lock <- renv::lockfile_read(file.path(project_dir, "renv.lock"))
lock_packages <- names(if (is.null(lock$Packages)) list() else lock$Packages)
requested_packages <- if (is.null(packages_to_restore)) lock_packages else intersect(packages_to_restore, lock_packages)

installed_now <- rownames(installed.packages(lib.loc = unique(c(library_dir, system_lib)), noCache = TRUE))
available_repo_packages <- tryCatch(
  rownames(available.packages(repos = getOption("repos"))),
  error = function(e) {
    write_root_cause(
      sprintf("Repository metadata lookup failed before restore: %s", conditionMessage(e)),
      c("Configured repos:", paste(names(getOption("repos")), getOption("repos"), sep = "=", collapse = ", "))
    )
    stop(e)
  }
)
missing_from_repo <- setdiff(requested_packages, union(installed_now, available_repo_packages))
if (length(missing_from_repo)) {
  write_root_cause(
    sprintf("Requested packages unavailable before restore: %s", paste(missing_from_repo, collapse = ", ")),
    c(
      sprintf("Requested package count: %d", length(requested_packages)),
      sprintf("Already installed/linked package count: %d", length(installed_now)),
      sprintf("Repo-visible package count: %d", length(available_repo_packages))
    )
  )
  stop(sprintf("requested packages unavailable before restore: %s", paste(missing_from_repo, collapse = ", ")))
}

restore_error <- tryCatch({
  renv::restore(
    project = project_dir,
    lockfile = file.path(project_dir, "renv.lock"),
    library = library_dir,
    packages = packages_to_restore,
    prompt = FALSE,
    clean = clean_restore
  )
  NULL
}, error = function(e) e)

if (!is.null(restore_error)) {
  write_root_cause(
    sprintf("renv::restore failed: %s", conditionMessage(restore_error)),
    c("See restore.log for full package cascade.")
  )
  stop(restore_error)
}

project_library <- normalizePath(library_dir, winslash = "/", mustWork = FALSE)
installed <- as.data.frame(
  installed.packages(
    lib.loc = project_library,
    fields = c("Priority", "Repository")
  ),
  stringsAsFactors = FALSE
)

installed_out <- data.frame(
  package_name = installed[, "Package"],
  package_version = installed[, "Version"],
  library_path = project_library,
  priority = if ("Priority" %in% colnames(installed)) installed[, "Priority"] else "",
  repository = if ("Repository" %in% colnames(installed)) installed[, "Repository"] else "",
  stringsAsFactors = FALSE
)

installed_out <- installed_out[order(tolower(installed_out$package_name)), ]
write.csv(installed_out, file.path(output_dir, "installed-packages.csv"), row.names = FALSE)
writeLines(normalizePath(project_library, winslash = "/", mustWork = FALSE), file.path(output_dir, "library-path.txt"))
capture.output(sessionInfo(), file = file.path(output_dir, "session-info.txt"))
capture.output(renv::status(project = project_dir), file = file.path(output_dir, "renv-status.txt"))
writeLines(sprintf("platform=%s", platform), file.path(output_dir, "restore-platform.txt"))
