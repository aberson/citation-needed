-- 0002 — artifact-level composite lands on the run row (plan.md §4.4, Step 4).
--
-- The committed unit of a review is the RUN, so the artifact composite (mean of
-- per-choice majority-label weights rescaled (mean+1)/2*100), its band, and the
-- interpretation-guide version are stored on review_runs at `cite review commit`
-- time. Per-choice inputs stay on scores. All three are NULL until the run commits.
-- The ONE implementation of the math lives in src/citation_needed/review.py.
--
-- schema.sql is updated in the same change (columns appended after `notes`, matching
-- ALTER TABLE's append position) so a brand-new DB and a migrated DB converge on the
-- same table shape; new DBs land on PRAGMA user_version = 2 directly.

ALTER TABLE review_runs ADD COLUMN composite REAL
    CHECK (composite IS NULL OR composite BETWEEN 0 AND 100);
ALTER TABLE review_runs ADD COLUMN composite_band TEXT
    CHECK (composite_band IS NULL OR composite_band IN
        ('strong', 'adequate', 'weak', 'unsupported'));
ALTER TABLE review_runs ADD COLUMN interpretation_guide_version TEXT;
