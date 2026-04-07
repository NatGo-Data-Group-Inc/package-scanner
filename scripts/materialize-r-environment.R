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
  CRAN = "https://cloud.r-project.org",
  RSPM = "https://packagemanager.posit.co/all/latest"
))
Sys.setenv(
  RENV_PATHS_CACHE = cache_dir,
  RENV_CONFIG_CACHE_SYMLINKS = "FALSE",
  RENV_CONFIG_PAK_ENABLED = "FALSE"
)
.libPaths(unique(c(library_dir, .libPaths())))

if (!requireNamespace("renv", quietly = TRUE)) {
  install.packages("renv")
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

renv::restore(
  project = project_dir,
  lockfile = file.path(project_dir, "renv.lock"),
  library = library_dir,
  packages = packages_to_restore,
  prompt = FALSE,
  clean = clean_restore
)

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
