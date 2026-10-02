#include "pgfire.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void stop(int signal_number) { (void)signal_number; pf_stopping = 1; }

static int checkpoint(pf_store *s, const char *file, const char *lsn,
                      const char *system_id, const char *tli, const char *expected_end) {
    int ok;
    uint64_t start = pf_parse_lsn(lsn, &ok);
    if (!getenv("PGFIRE_OWNER_TOKEN") || !ok || atoi(tli) < 1)
        return pf_fail(s, "checkpoint requires an existing owner and valid metadata");
    if (pf_claim(s)) return -1;
    uint64_t durable = pf_parse_lsn(pf_text(s->state, "end_lsn"), &ok);
    int covers = ok && start >= durable;
    const char *recovery_fork = getenv("PGFIRE_RECOVERY_FORK_LSN");
    if (!covers && recovery_fork && atoi(tli) > pf_number(s->state, "timeline")) {
        int fork_ok, base_ok;
        uint64_t fork = pf_parse_lsn(recovery_fork, &fork_ok);
        uint64_t base = pf_parse_lsn(pf_text(s->state, "base_lsn"), &base_ok);
        /* PostgreSQL has replayed the verified chain. A partial last WAL record
           can extend beyond its recovery fork; no complete COMMIT is lost. */
        covers = ok && fork_ok && base_ok && fork >= base && fork <= start && fork < durable;
    }
    if (!covers || strcmp(pf_text(s->state, "end_lsn"), expected_end) ||
        strcmp(pf_text(s->state, "system_id"), system_id) ||
        atoi(tli) < pf_number(s->state, "timeline"))
        return pf_fail(s, "checkpoint does not cover the current durable prefix");
    char id[PF_ID_SIZE];
    if (pf_upload_file(s, file, id)) return -1;
    pf_string(s->state, "base", id);
    pf_string(s->state, "base_lsn", lsn);
    pf_string(s->state, "end_lsn", lsn);
    pf_string(s->state, "head", "");
    /* The clean snapshot includes timeline history files. */
    pf_string(s->state, "histories", "");
    pf_integer(s->state, "timeline", atoi(tli));
    pf_integer(s->state, "wal_records", 0);
    return pf_publish(s, NULL, NULL);
}

static int bootstrap(pf_store *s, const char *file, const char *lsn, const char *system_id, const char *tli) {
    int ok;
    (void)pf_parse_lsn(lsn, &ok);
    if (!ok || atoi(tli) < 1 || !*system_id) return pf_fail(s, "invalid bootstrap metadata");
    if (pf_claim(s)) return -1;
    if (*pf_text(s->state, "base")) return pf_fail(s, "namespace is already initialized");
    char id[PF_ID_SIZE];
    if (pf_upload_file(s, file, id)) return -1;
    pf_string(s->state, "base", id);
    pf_string(s->state, "base_lsn", lsn);
    pf_string(s->state, "end_lsn", lsn);
    pf_string(s->state, "system_id", system_id);
    pf_string(s->state, "head", "");
    pf_integer(s->state, "timeline", atoi(tli));
    pf_integer(s->state, "format_version", 1);
    pf_integer(s->state, "wal_records", 0);
    if (pf_publish(s, NULL, NULL)) return -1;
    return pf_release(s);
}

static void usage(void) {
    fputs("pgfire inspect\n"
          "pgfire acquire  (prints an owner transfer token)\n"
          "pgfire release  (requires PGFIRE_OWNER_TOKEN; stop SQL service first)\n"
          "pgfire bootstrap ARCHIVE BASE_LSN SYSTEM_ID TIMELINE\n"
          "pgfire checkpoint ARCHIVE BASE_LSN SYSTEM_ID TIMELINE EXPECTED_END\n"
          "pgfire stream  (connection string from PGFIRE_CONNINFO)\n"
          "pgfire download-base FILE\n"
          "pgfire restore-wal DIRECTORY\n"
          "pgfire put-object FILE\n"
          "pgfire get-object ID FILE\n"
          "Configure FIRESTORE_EMULATOR_HOST or PGFIRE_ACCESS_TOKEN, PGFIRE_PROJECT,\n"
          "PGFIRE_DATABASE, and PGFIRE_NAMESPACE via the environment.\n", stderr);
}

int main(int argc, char **argv) {
    if (argc < 2 || !strcmp(argv[1], "--help")) { usage(); return argc < 2 ? 2 : 0; }
    struct sigaction action = {0};
    action.sa_handler = stop;
    sigemptyset(&action.sa_mask);
    sigaction(SIGINT, &action, NULL); sigaction(SIGTERM, &action, NULL);
    signal(SIGPIPE, SIG_IGN);
    pf_store store;
    if (pf_open(&store)) { fprintf(stderr, "pgfire: %s\n", store.error); return 1; }
    int rc = -1;
    if (!strcmp(argv[1], "inspect") && argc == 2) {
        rc = pf_load(&store);
        if (!rc) puts(json_object_to_json_string_ext(store.state, JSON_C_TO_STRING_PRETTY));
    } else if (!strcmp(argv[1], "acquire") && argc == 2) {
        rc = pf_claim(&store);
        if (!rc) puts(store.owner);
    } else if (!strcmp(argv[1], "release") && argc == 2) {
        rc = getenv("PGFIRE_OWNER_TOKEN") ? pf_claim(&store) : pf_fail(&store, "owner token required");
        if (!rc) rc = pf_release(&store);
    } else if (!strcmp(argv[1], "bootstrap") && argc == 6) {
        rc = bootstrap(&store, argv[2], argv[3], argv[4], argv[5]);
    } else if (!strcmp(argv[1], "checkpoint") && argc == 7) {
        rc = checkpoint(&store, argv[2], argv[3], argv[4], argv[5], argv[6]);
    } else if (!strcmp(argv[1], "stream") && argc == 2) {
        const char *conninfo = getenv("PGFIRE_CONNINFO");
        rc = conninfo ? pf_stream(&store, conninfo) : pf_fail(&store, "PGFIRE_CONNINFO is required");
    } else if (!strcmp(argv[1], "download-base") && argc == 3) {
        rc = getenv("PGFIRE_OWNER_TOKEN") ? pf_claim(&store) : pf_load(&store);
        if (!rc) rc = pf_download(&store, pf_text(store.state, "base"), argv[2]);
    } else if (!strcmp(argv[1], "restore-wal") && argc == 3) {
        rc = pf_restore(&store, argv[2]);
    } else if (!strcmp(argv[1], "put-object") && argc == 3) {
        char id[PF_ID_SIZE];
        rc = pf_claim(&store);
        if (!rc) rc = pf_upload_file(&store, argv[2], id);
        if (!rc) rc = pf_release(&store);
        if (!rc) puts(id);
    } else if (!strcmp(argv[1], "get-object") && argc == 4) {
        rc = pf_download(&store, argv[2], argv[3]);
    } else { usage(); pf_fail(&store, "invalid command"); }
    if (rc) fprintf(stderr, "pgfire: %s\n", store.error);
    /* Keep failed ownership until expiry: no unsafe takeover after an ambiguous write. */
    pf_close(&store);
    return rc ? 1 : 0;
}
