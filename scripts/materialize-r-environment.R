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

dir.create(project_dir, recursive = TRUE, showWarnings = FALSE)
dir.create(cache_dir, recursive = TRUE, showWarnings = FALSE)
dir.create(output_dir, recursive = TRUE, showWarnings = FALSE)
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

options(repos = c(CRAN = "https://cloud.r-project.org"))
Sys.setenv(
  RENV_PATHS_CACHE = cache_dir,
  RENV_CONFIG_CACHE_SYMLINKS = "FALSE",
  RENV_CONFIG_PAK_ENABLED = "FALSE"
)

if (!requireNamespace("renv", quietly = TRUE)) {
  install.packages("renv")
}

renv::consent(provided = TRUE)
setwd(project_dir)
renv::restore(project = project_dir, lockfile = file.path(project_dir, "renv.lock"), prompt = FALSE, clean = TRUE)

project_library <- .libPaths()[1]
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
