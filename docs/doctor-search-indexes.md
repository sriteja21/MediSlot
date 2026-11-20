# Doctor search indexes

The initializer applies missing indexes by name, so existing databases receive the same search indexes as fresh installs. Indexes are declared in `src/init_lambda/schema.sql` and ensured in `apply_schema_migrations()` in `src/init_lambda/handler.py`.

| Index | Query supported | Notes |
|---|---|---|
| `hospitals.idx_hospitals_city_id (city, hospital_id)` | Exact city filter and the hospital-to-doctor join | `city` uses the database's case-insensitive collation. |
| `doctors.idx_doctors_hospital (hospital_id)` | Hospital-to-doctor join | Supports joining search candidates to their hospital row. |
| `doctors.idx_doctors_name (last_name, first_name)` | Name ordering and doctor profile/name lookups | The current arbitrary substring predicate uses `LIKE '%term%'`, which a B-tree cannot seek; it remains a residual filter. |
| `doctors.idx_doctors_verified_experience (is_verified, experience_years, doctor_id)` | Verified-doctor restriction and experience sort | The direction of the experience sort may still require a filesort. |
| `doctor_specializations.idx_doctor_specializations_specialization (specialization_id, doctor_id)` | Exact specialisation filter and doctor join | Complements the primary key `(doctor_id, specialization_id)` used by profile lookups. |
| `consultation_types.idx_consultation_types_doctor_active (doctor_id, is_active)` | Active consultation-type join and minimum-fee aggregation per doctor | |
| `consultation_types.idx_consultation_types_active_fee (is_active, fee, doctor_id)` | Active consultation types ordered by fee | |
| `hospitals.uq_hospital_name_address (name, address)` | Hospital name lookups | Prefix matches can use the leading `name` column; the current arbitrary substring match cannot. |

City and specialisation filters span `hospitals` and `doctor_specializations`, so MySQL combines their per-table indexes; a single composite index cannot span both tables. Name and hospital filters intentionally retain substring matching. Adding FULLTEXT or changing those predicates to prefix-only matching would change search behavior and should be considered separately if query plans show that the candidate scan needs further optimization.

## Capture plans against the deployed MySQL schema

Run these commands using the same read-only database role and schema as the Patient API. The actual optimizer plan depends on the deployed MySQL version, statistics, and data distribution; no live RDS connection is available in this repository workspace, so an `EXPLAIN` result is deliberately not fabricated here.

```sql
EXPLAIN FORMAT=JSON
SELECT COUNT(DISTINCT d.doctor_id)
FROM doctors d
JOIN hospitals h ON h.hospital_id = d.hospital_id
JOIN consultation_types ct ON ct.doctor_id = d.doctor_id
LEFT JOIN doctor_specializations ds ON ds.doctor_id = d.doctor_id
LEFT JOIN specializations s ON s.specialization_id = ds.specialization_id
WHERE d.is_verified = TRUE
  AND ct.is_active = TRUE
  AND h.city = 'Bengaluru'
  AND EXISTS (
    SELECT 1
    FROM doctor_specializations dsf
    JOIN specializations sf ON sf.specialization_id = dsf.specialization_id
    WHERE dsf.doctor_id = d.doctor_id
      AND sf.name = 'Cardiology'
  );
```

```sql
EXPLAIN FORMAT=JSON
SELECT d.doctor_id,
       CONCAT_WS(' ', d.first_name, d.last_name) AS name,
       h.name AS hospital_name,
       MIN(ct.fee) AS lowest_fee
FROM doctors d
JOIN hospitals h ON h.hospital_id = d.hospital_id
JOIN consultation_types ct ON ct.doctor_id = d.doctor_id
WHERE d.is_verified = TRUE
  AND ct.is_active = TRUE
  AND CONCAT_WS(' ', d.first_name, d.last_name) LIKE '%smith%'
  AND h.name LIKE '%medical%'
GROUP BY d.doctor_id, d.first_name, d.last_name, h.name
ORDER BY lowest_fee ASC, d.last_name ASC, d.first_name ASC
LIMIT 10 OFFSET 0;
```

