#include "pgfire.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

int main(void) {
    pf_store first, second;
    assert(pf_open(&first) == 0);
    assert(pf_open(&second) == 0);
    assert(pf_claim(&first) == 0);
    assert(pf_claim(&second) != 0);
    assert(strstr(second.error, "unexpired owner") != NULL);
    /* Force the lease-generation transition that a timed-out takeover performs.
       The displaced owner still has a live local deadline and stale updateTime. */
    assert(pf_load(&second) == 0);
    second.held = 1;
    second.deadline = first.deadline;
    pf_integer(second.state, "epoch", pf_number(second.state, "epoch") + 1);
    assert(pf_publish(&second, NULL, NULL) == 0);
    assert(pf_renew(&first) != 0);
    assert(first.held == 0);
    assert(pf_load(&first) == 0);
    assert(strcmp(pf_text(first.state, "owner"), second.owner) == 0);
    /* Never acknowledge a chain longer than recovery can read. */
    pf_integer(second.state, "wal_records", PF_MAX_WAL_RECORDS);
    struct json_object *record = json_object_new_object();
    assert(pf_publish(&second, "limit-probe", record) != 0);
    assert(strstr(second.error, "retention limit") != NULL);
    json_object_put(record);
    assert(pf_release(&second) == 0);
    pf_close(&second);
    pf_close(&first);
    puts("stale owner rejected by Firestore updateTime fencing");
    return 0;
}
