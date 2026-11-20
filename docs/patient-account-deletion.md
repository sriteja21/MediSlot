# Patient account deletion

Milestone 4 does not create appointments. Deleting a patient account therefore deletes the patient user/profile and revokes all refresh-token sessions. The Patient API refuses deletion if appointment records are already present, so a future workflow cannot silently orphan them.

## Milestone 5 policy

When appointment booking is introduced, an account with upcoming appointments must not be hard-deleted. The account must be deactivated/anonymised while retaining the appointment rows and their required audit/payment relationships. Completed or historical appointments must likewise remain referentially intact under the retention policy. The Milestone 5 deletion workflow must revoke refresh tokens, prevent further login, and anonymise personal fields transactionally with the appointment-preservation operation; the exact retained fields and retention period must be agreed before that milestone ships.

