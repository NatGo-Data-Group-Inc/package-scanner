args <- commandArgs(trailingOnly = TRUE)

get_arg <- function(name) {
  idx <- match(name, args)
  if (is.na(idx) || idx == length(args)) {
    stop(paste("Missing required argument:", name), call. = FALSE)
  }
  args[[idx + 1]]
}

has_arg <- function(name) {
  name %in% args
}

`%||%` <- function(lhs, rhs) {
  if (is.null(lhs) || (length(lhs) == 1 && is.na(lhs)) || identical(lhs, "")) rhs else lhs
}

lock_file <- if (has_arg("--lock-file")) normalizePath(get_arg("--lock-file"), mustWork = TRUE) else NULL
requested_packages_file <- if (has_arg("--requested-packages-file")) normalizePath(get_arg("--requested-packages-file"), mustWork = TRUE) else NULL
project_dir <- normalizePath(get_arg("--project-dir"), mustWork = FALSE)
cache_dir <- normalizePath(get_arg("--cache-dir"), mustWork = FALSE)
output_dir <- normalizePath(get_arg("--output-dir"), mustWork = FALSE)
platform <- get_arg("--platform")

library_dir <- if ("--library-dir" %in% args) normalizePath(get_arg("--library-dir"), mustWork = FALSE) else normalizePath(file.path(output_dir, "library"), mustWork = FALSE)
packages_file <- if ("--packages-file" %in% args) normalizePath(get_arg("--packages-file"), mustWork = TRUE) else NULL
clean_restore <- if ("--clean" %in% args) tolower(get_arg("--clean")) == "true" else FALSE

if (is.null(lock_file) == is.null(requested_packages_file)) {
  stop("Pass exactly one of --lock-file or --requested-packages-file", call. = FALSE)
}

dir.create(project_dir, recursive = TRUE, showWarnings = FALSE)
dir.create(cache_dir, recursive = TRUE, showWarnings = FALSE)
dir.create(output_dir, recursive = TRUE, showWarnings = FALSE)
dir.create(library_dir, recursive = TRUE, showWarnings = FALSE)
if (!is.null(lock_file)) {
  file.copy(lock_file, file.path(project_dir, "renv.lock"), overwrite = TRUE)
}

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
system_lib <- normalizePath(
  Sys.getenv("R_SYSTEM_LIBRARY", unset = R.home("library")),
  winslash = "/",
  mustWork = FALSE
)
Sys.setenv(
  RENV_PATHS_CACHE = cache_dir,
  RENV_CONFIG_CACHE_SYMLINKS = "FALSE",
  RENV_CONFIG_PAK_ENABLED = "FALSE",
  RENV_CONFIG_EXTERNAL_LIBRARIES = system_lib
)
.libPaths(unique(c(library_dir, system_lib, .libPaths())))

if (!requireNamespace("renv", quietly = TRUE)) {
  install.packages("renv")
}
if (!requireNamespace("jsonlite", quietly = TRUE)) {
  install.packages("jsonlite")
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

installed_package_names <- function() {
  rownames(installed.packages(lib.loc = unique(c(library_dir, system_lib)), noCache = TRUE))
}

missing_requested_packages <- function() {
  setdiff(requested_packages, installed_package_names())
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

normalize_repositories <- function(repo_value) {
  if (is.null(repo_value)) {
    return(getOption("repos"))
  }
  if (is.list(repo_value) && !is.null(names(repo_value)) && all(vapply(repo_value, is.character, logical(1), USE.NAMES = FALSE))) {
    repos <- unlist(repo_value, use.names = TRUE)
    return(repos[nzchar(repos)])
  }
  if (is.list(repo_value)) {
    repos <- vapply(
      repo_value,
      function(entry) {
        if (is.list(entry)) {
          entry$url %||% entry$URL %||% ""
        } else {
          ""
        }
      },
      character(1)
    )
    repo_names <- vapply(
      repo_value,
      function(entry) {
        if (is.list(entry)) {
          entry$name %||% entry$Name %||% ""
        } else {
          ""
        }
      },
      character(1)
    )
    names(repos) <- repo_names
    repos <- repos[nzchar(names(repos)) & nzchar(repos)]
    if (length(repos)) {
      return(repos)
    }
  }
  getOption("repos")
}

build_requested_entry <- function(entry) {
  if (is.character(entry)) {
    return(list(
      name = unname(entry),
      source = "Repository",
      ref = unname(entry)
    ))
  }
  if (!is.list(entry)) {
    stop("Unsupported requested package entry type", call. = FALSE)
  }
  name <- entry$name %||% entry$Package
  if (is.null(name) || !nzchar(name)) {
    stop("Requested package entry is missing name", call. = FALSE)
  }
  source <- entry$source %||% entry$Source %||% "Repository"
  ref <- entry$ref
  if (is.null(ref) || !nzchar(ref)) {
    if (tolower(source) == "github") {
      remote_username <- entry$remote_username %||% entry$RemoteUsername
      remote_repo <- entry$remote_repo %||% entry$RemoteRepo
      remote_ref <- entry$remote_ref %||% entry$RemoteRef
      if (!is.null(remote_username) && !is.null(remote_repo)) {
        ref <- sprintf(
          "%s/%s%s",
          remote_username,
          remote_repo,
          if (!is.null(remote_ref) && nzchar(remote_ref)) paste0("@", remote_ref) else ""
        )
      }
    }
  }
  if (is.null(ref) || !nzchar(ref)) {
    ref <- name
  }
  list(
    name = name,
    source = source,
    ref = ref
  )
}

write_generated_lockfile <- function() {
  generated_lockfile <- file.path(project_dir, "renv.lock")
  if (!file.exists(generated_lockfile)) {
    stop("Expected generated renv.lock after materialization", call. = FALSE)
  }
  file.copy(generated_lockfile, file.path(output_dir, "renv.lock"), overwrite = TRUE)
}

lockfile_payload <- function(lockfile_path) {
  if (is.null(lockfile_path) || !file.exists(lockfile_path)) {
    return(NULL)
  }
  jsonlite::fromJSON(lockfile_path, simplifyVector = FALSE)
}

lockfile_has_bioconductor <- function(lockfile_path) {
  payload <- lockfile_payload(lockfile_path)
  !is.null(payload) && !is.null(payload$Bioconductor)
}

bootstrap_packages_for_lockfile <- function(lockfile_path) {
  packages <- character()
  if (lockfile_has_bioconductor(lockfile_path)) {
    # renv bootstraps Bioconductor support through BiocManager during restore.
    packages <- c(packages, "BiocManager")
  }
  unique(packages)
}

lockfile_package_record <- function(lockfile_path, package_name) {
  payload <- lockfile_payload(lockfile_path)
  packages <- payload$Packages
  if (is.null(packages) || is.null(packages[[package_name]])) {
    return(NULL)
  }
  packages[[package_name]]
}

count_lockfile_packages <- function(lockfile_path) {
  lock <- lockfile_payload(lockfile_path)
  packages <- lock$Packages
  if (is.null(packages)) {
    return(0L)
  }
  length(packages)
}

mirror_materialized_library_into_project <- function() {
  project_library <- renv::paths$library(project = project_dir)
  dir.create(project_library, recursive = TRUE, showWarnings = FALSE)
  package_dirs <- list.files(library_dir, all.files = FALSE, no.. = TRUE)
  for (pkg in package_dirs) {
    src <- file.path(library_dir, pkg)
    dest <- file.path(project_library, pkg)
    if (dir.exists(dest) || file.exists(dest)) {
      unlink(dest, recursive = TRUE, force = TRUE)
    }
    linked <- tryCatch(file.symlink(src, dest), warning = function(w) FALSE, error = function(e) FALSE)
    if (!isTRUE(linked)) {
      ok <- file.copy(src, dest, recursive = TRUE)
      if (!isTRUE(ok)) {
        stop(sprintf("Failed to mirror package %s into project library", pkg), call. = FALSE)
      }
    }
  }
  project_library
}

package_installed_in_library <- function(package_name, lib = library_dir) {
  if (!dir.exists(lib)) {
    return(FALSE)
  }
  package_name %in% rownames(installed.packages(lib.loc = lib, noCache = TRUE))
}

ensure_bootstrap_packages <- function(packages, include_in_generated_lock = FALSE) {
  packages <- unique(packages[nzchar(packages)])
  if (!length(packages)) {
    return(invisible(NULL))
  }

  missing <- packages[!vapply(packages, package_installed_in_library, logical(1), USE.NAMES = FALSE)]
  if (!length(missing)) {
    return(invisible(NULL))
  }

  message(sprintf(
    "Installing restore-bootstrap packages into the realized library: %s",
    paste(missing, collapse = ", ")
  ))
  install.packages(missing, lib = library_dir, dependencies = TRUE)

  if (include_in_generated_lock) {
    snapshot_requested_environment()
    write_generated_lockfile()
  }
}

ensure_lockfile_packages_restored <- function(packages, lockfile_path) {
  packages <- unique(packages[nzchar(packages)])
  if (!length(packages)) {
    return(invisible(NULL))
  }

  to_restore <- Filter(
    f = function(package_name) {
      record <- lockfile_package_record(lockfile_path, package_name)
      if (is.null(record)) {
        return(FALSE)
      }
      installed <- tryCatch(
        installed.packages(lib.loc = library_dir, noCache = TRUE),
        error = function(e) NULL
      )
      if (is.null(installed) || !(package_name %in% rownames(installed))) {
        return(TRUE)
      }
      installed_version <- installed[package_name, "Version"]
      recorded_version <- record$Version %||% ""
      !identical(installed_version, recorded_version)
    },
    x = packages
  )

  if (!length(to_restore)) {
    return(invisible(NULL))
  }

  message(sprintf(
    "Restoring lockfile-bootstrap packages into the realized library: %s",
    paste(to_restore, collapse = ", ")
  ))
  renv::restore(
    project = project_dir,
    lockfile = lockfile_path,
    library = library_dir,
    packages = unname(to_restore),
    prompt = FALSE,
    clean = FALSE
  )
}

snapshot_requested_environment <- function() {
  renv::snapshot(
    project = project_dir,
    library = library_dir,
    lockfile = file.path(project_dir, "renv.lock"),
    prompt = FALSE,
    type = "all"
  )

  installed_count <- nrow(installed.packages(lib.loc = unique(c(library_dir, system_lib)), noCache = TRUE))
  lock_count <- count_lockfile_packages(file.path(project_dir, "renv.lock"))

  if (lock_count >= max(length(requested_packages), floor(installed_count * 0.8))) {
    return(invisible(NULL))
  }

  message(
    sprintf(
      "Generated lockfile only captured %d packages for %d installed packages; mirroring materialized library into the project library and retrying snapshot.",
      lock_count,
      installed_count
    )
  )

  project_library <- mirror_materialized_library_into_project()
  .libPaths(unique(c(project_library, library_dir, system_lib, .libPaths())))
  renv::snapshot(
    project = project_dir,
    library = project_library,
    lockfile = file.path(project_dir, "renv.lock"),
    prompt = FALSE,
    type = "all",
    force = TRUE
  )

  lock_count <- count_lockfile_packages(file.path(project_dir, "renv.lock"))
  if (lock_count < max(length(requested_packages), floor(installed_count * 0.8))) {
    stop(
      sprintf(
        "Generated renv.lock still appears truncated after retry (%d packages for %d installed packages).",
        lock_count,
        installed_count
      ),
      call. = FALSE
    )
  }
}

input_mode <- if (is.null(lock_file)) "requested" else "lockfile"
lock_entries <- list()
requested_package_refs <- character()
requested_system_seed_packages <- character()

if (identical(input_mode, "lockfile")) {
  lock <- renv::lockfile_read(file.path(project_dir, "renv.lock"))
  lock_entries <- if (is.null(lock$Packages)) list() else lock$Packages
  lock_packages <- names(lock_entries)
  requested_packages <- if (is.null(packages_to_restore)) lock_packages else intersect(packages_to_restore, lock_packages)
  requested_package_refs <- setNames(requested_packages, requested_packages)
} else {
  requested_manifest <- jsonlite::fromJSON(requested_packages_file, simplifyVector = FALSE)
  requested_manifest_output <- normalizePath(file.path(output_dir, "requested-packages.json"), mustWork = FALSE)
  if (!identical(requested_packages_file, requested_manifest_output)) {
    file.copy(requested_packages_file, requested_manifest_output, overwrite = TRUE)
  }
  manifest_packages <- requested_manifest$packages %||% list()
  requested_entries <- lapply(manifest_packages, build_requested_entry)
  if (!is.null(packages_to_restore)) {
    requested_entries <- Filter(function(entry) entry$name %in% packages_to_restore, requested_entries)
  }
  requested_packages <- vapply(requested_entries, function(entry) entry$name, character(1))
  requested_package_refs <- stats::setNames(vapply(requested_entries, function(entry) entry$ref, character(1)), requested_packages)
  lock_entries <- stats::setNames(lapply(requested_entries, function(entry) list(Source = entry$source)), requested_packages)
  manifest_repos <- normalize_repositories(requested_manifest$repositories %||% requested_manifest$Repositories)
  options(repos = manifest_repos)
}

requested_system_seed_packages <- requested_packages[
  vapply(requested_packages, package_installed_in_library, logical(1), lib = system_lib, USE.NAMES = FALSE)
]

installed_now <- installed_package_names()
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
repo_requested_packages <- Filter(
  f = function(pkg) {
    entry <- lock_entries[[pkg]]
    !is.null(entry) && identical(entry$Source, "Repository")
  },
  x = requested_packages
)
missing_from_repo <- setdiff(repo_requested_packages, union(installed_now, available_repo_packages))
if (length(missing_from_repo)) {
  write_root_cause(
    sprintf("Requested packages unavailable before restore: %s", paste(missing_from_repo, collapse = ", ")),
    c(
      sprintf("Requested package count: %d", length(requested_packages)),
      sprintf("Repository-sourced requested package count: %d", length(repo_requested_packages)),
      sprintf("Already installed/linked package count: %d", length(installed_now)),
      sprintf("Repo-visible package count: %d", length(available_repo_packages))
    )
  )
  stop(sprintf("requested packages unavailable before restore: %s", paste(missing_from_repo, collapse = ", ")))
}

run_restore <- function(pkgs = packages_to_restore, clean = clean_restore) {
  if (identical(input_mode, "lockfile")) {
    return(tryCatch({
      ensure_lockfile_packages_restored(
        "renv",
        file.path(project_dir, "renv.lock")
      )
      ensure_bootstrap_packages(
        bootstrap_packages_for_lockfile(file.path(project_dir, "renv.lock")),
        include_in_generated_lock = FALSE
      )
      renv::restore(
        project = project_dir,
        lockfile = file.path(project_dir, "renv.lock"),
        library = library_dir,
        packages = pkgs,
        prompt = FALSE,
        clean = clean
      )
      NULL
    }, error = function(e) e))
  }

  tryCatch({
    if (!file.exists(file.path(project_dir, ".Rprofile"))) {
      renv::init(project = project_dir, bare = TRUE)
    }
    renv::settings$snapshot.type("all", project = project_dir)
    refs <- if (is.null(pkgs)) unname(requested_package_refs) else unname(requested_package_refs[names(requested_package_refs) %in% pkgs])
    if (length(requested_system_seed_packages)) {
      seed_refs <- unname(requested_package_refs[names(requested_package_refs) %in% requested_system_seed_packages])
      refs <- setdiff(refs, seed_refs)
    }
    if (!length(refs)) {
      refs <- character()
    }
    if (length(refs)) {
      renv::install(
        refs,
        project = project_dir,
        library = library_dir,
        prompt = FALSE
      )
    }
    snapshot_requested_environment()
    ensure_lockfile_packages_restored(
      "renv",
      file.path(project_dir, "renv.lock")
    )
    ensure_bootstrap_packages(
      bootstrap_packages_for_lockfile(file.path(project_dir, "renv.lock")),
      include_in_generated_lock = TRUE
    )
    write_generated_lockfile()
    NULL
  }, error = function(e) e)
}

restore_error <- run_restore()
retry_notes <- character()
if (!is.null(restore_error)) {
  remaining <- missing_requested_packages()
  if (length(remaining)) {
    retry_notes <- c(
      retry_notes,
      sprintf(
        "Initial restore failed; retrying %d still-missing packages after dependency materialization.",
        length(remaining)
      ),
      sprintf("Retry package set: %s", paste(remaining, collapse = ", "))
    )
    message(retry_notes[1])
    message(retry_notes[2])
    retry_error <- run_restore(pkgs = remaining, clean = FALSE)
    if (is.null(retry_error)) {
      restore_error <- NULL
    } else {
      restore_error <- retry_error
      remaining <- missing_requested_packages()
      if (length(remaining)) {
        retry_notes <- c(
          retry_notes,
          sprintf("Packages still missing after retry: %s", paste(remaining, collapse = ", "))
        )
      }
    }
  }
}

if (identical(input_mode, "requested") && is.null(restore_error)) {
  write_generated_lockfile()
}

if (!is.null(restore_error)) {
  write_root_cause(
    sprintf("%s failed: %s", if (identical(input_mode, "requested")) "requested package materialization" else "renv::restore", conditionMessage(restore_error)),
    c(retry_notes, "See restore.log for full package cascade.")
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
