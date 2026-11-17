# Stage 1, the parts done in R (called by src/stage1.py; not run on its own).
#
# 1. Right truncation: SurvTrunc::cdfDT (Efron–Petrosian NPMLE for truncated data) with left bound 0 and right bound
#    the time from the window's end to the corpus date, on the full sample and the cohort.
# 2. Competing risks on the cohort: cmprsk::cuminc (cumulative incidence by channel; there is no censoring, every matter
#    was acted on) and cmprsk::crr (Fine–Gray subdistribution hazards for each main channel).
#
# Usage: Rscript src/stage1.R <input.csv> <output dir>
#   input columns: t (months, > 0), r (months to the corpus date), cohort (0/1), channel (text), x_* covariates.
# Writes: truncation_curve.csv, truncation_summary.csv, cif_curves.csv, cif_at.csv, finegray.csv.

suppressPackageStartupMessages({
  library(survival)
  library(SurvTrunc)
  library(cmprsk)
})

pdf(NULL)  # no stray Rplots.pdf
args <- commandArgs(trailingOnly = TRUE)
input <- args[1]
outdir <- args[2]
d <- read.csv(input, stringsAsFactors = FALSE)

quantile_of <- function(time, cdf, p) {
  # The smallest time at which the step CDF reaches p.
  hit <- which(cdf >= p)
  if (length(hit) == 0) return(NA_real_)
  time[min(hit)]
}

# ---- 1. truncation --------------------------------------------------------------------------------------------
curves <- list()
summary_rows <- list()
for (sample_name in c("full", "cohort")) {
  s <- if (sample_name == "full") d else d[d$cohort == 1, ]
  fit <- cdfDT(s$t, rep(0, nrow(s)), s$r, display = FALSE)
  naive_t <- sort(unique(s$t))
  naive_cdf <- ecdf(s$t)(naive_t)
  curves[[length(curves) + 1]] <- data.frame(sample = sample_name, estimator = "adjusted",
                                             time = fit$time, cdf = fit$F)
  curves[[length(curves) + 1]] <- data.frame(sample = sample_name, estimator = "naive",
                                             time = naive_t, cdf = naive_cdf)
  for (p in c(0.25, 0.5, 0.75, 0.9)) {
    summary_rows[[length(summary_rows) + 1]] <- data.frame(
      sample = sample_name, n = nrow(s), quantile = p,
      naive = quantile_of(naive_t, naive_cdf, p), adjusted = quantile_of(fit$time, fit$F, p),
      iterations = fit$n.iterations, converged = !isTRUE(fit$max.iter_reached))
  }
}
write.csv(do.call(rbind, curves), file.path(outdir, "truncation_curve.csv"), row.names = FALSE)
write.csv(do.call(rbind, summary_rows), file.path(outdir, "truncation_summary.csv"), row.names = FALSE)

# ---- 2. competing risks on the cohort ---------------------------------------------------------------------------
c <- d[d$cohort == 1, ]
levels_ <- sort(unique(c$channel))
code <- match(c$channel, levels_)
ci <- cuminc(ftime = c$t, fstatus = code, cencode = 0)
cif_rows <- list()
at_rows <- list()
times <- c(12, 24, 36, 48, 60, 84, 96, 120)
for (k in seq_along(levels_)) {
  key <- paste("1", k)
  if (is.null(ci[[key]])) next
  cif_rows[[length(cif_rows) + 1]] <- data.frame(channel = levels_[k], time = ci[[key]]$time, cif = ci[[key]]$est)
  for (tt in c(times, max(c$t))) {
    idx <- max(which(ci[[key]]$time <= tt), 1)
    at_rows[[length(at_rows) + 1]] <- data.frame(channel = levels_[k], time = tt, cif = ci[[key]]$est[idx],
                                                 var = ci[[key]]$var[idx])
  }
}
write.csv(do.call(rbind, cif_rows), file.path(outdir, "cif_curves.csv"), row.names = FALSE)
write.csv(do.call(rbind, at_rows), file.path(outdir, "cif_at.csv"), row.names = FALSE)

xcols <- grep("^x_", names(c), value = TRUE)
X <- as.matrix(c[, xcols, drop = FALSE])
fg_rows <- list()
for (target in intersect(c("SURVEILLANCE", "COMPLAINT", "INSPECTION", "REFERRAL"), levels_)) {
  fit <- tryCatch(crr(ftime = c$t, fstatus = code, cov1 = X, failcode = match(target, levels_), cencode = 0,
                      maxiter = 50),
                  error = function(e) NULL)
  if (is.null(fit)) next
  se <- sqrt(diag(fit$var))
  fg_rows[[length(fg_rows) + 1]] <- data.frame(channel = target, covariate = xcols, coef = fit$coef,
                                               shr = exp(fit$coef), se = se,
                                               p = 2 * pnorm(-abs(fit$coef / se)),
                                               converged = fit$converged, n_events = sum(c$channel == target))
}
write.csv(do.call(rbind, fg_rows), file.path(outdir, "finegray.csv"), row.names = FALSE)
cat("stage1.R done:", nrow(d), "matters,", nrow(c), "in the cohort\n")
