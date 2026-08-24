# Support runbook: password reset (internal)

Last reviewed: 2026-07-10. Owner: Support Ops.

1. Verify the requester through the secondary email on file.
2. In Aurora Helpdesk, open the requester's profile and click
   "Force password reset".
3. The reset link expires after 30 minutes; tell the requester.
4. Escalate to Support Ops on-call if the profile has no secondary email.

Reset volume is tracked in Starlight DB table `reset_audit`.
