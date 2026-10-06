Match history must carry the ELO delta of a ranked match, not just the rating
after it.

`backend/matchHistory.js` stores `ratingAAfter` / `ratingBAfter`. The client
cannot display "+18 / -18" next to a past match, because the rating before the
match is never kept. The server already computes both deltas when it updates
the ratings and then throws them away.

What has to be true when you are done:

1. `backend/matchHistory.js` emits `ratingADelta` and `ratingBDelta` on the
   record. Both are `null` when the match is not ranked, or when the caller did
   not supply the ratings. The existing fields keep their meaning.
2. `backend/test/matchHistory.test.js` covers the new fields: a ranked match
   with both deltas, and the null cases. The existing assertions still hold.
3. The real ranked call site in `backend/server.js` passes what the shaper now
   needs, so a real ranked match records true deltas rather than nulls.
4. The record is persisted by `backend/supabaseClient.js` through an RPC whose
   parameters are declared in a SQL migration under `supabase/migrations/`. The
   two new columns need their own migration file, numbered after the last one,
   and the client has to send them.
5. The suite stays green. Run it with:
   node --test "backend/test/!(bots|physics).test.js"
   219 tests pass today; none of them may break.

Constraints: this is a real repository. Read before you edit — `backend/server.js`
is 138 KB, never read it whole, search it. Do not reformat files you are not
changing, and do not commit.
