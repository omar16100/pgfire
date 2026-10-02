#include "pgfire.h"
#include <libpq-fe.h>
#include <errno.h>
#include <inttypes.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define WAL_SEGMENT (16u * 1024u * 1024u)

static int feedback(pf_store *s, PGconn *conn, uint64_t durable) {
    unsigned char reply[34] = {'r'};
    pf_set_u64(reply + 1, durable);
    pf_set_u64(reply + 9, durable);
    /* Firestore stores bytes; it does not apply WAL. Never claim remote_apply. */
    pf_set_u64(reply + 17, 0);
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    uint64_t timestamp = (uint64_t)(ts.tv_sec - 946684800) * UINT64_C(1000000) + (uint64_t)ts.tv_nsec / 1000;
    pf_set_u64(reply + 25, timestamp);
    if (PQputCopyData(conn, (const char *)reply, sizeof(reply)) != 1 || PQflush(conn) < 0)
        return pf_fail(s, "replication acknowledgement failed");
    return 0;
}

static int history(pf_store *s, PGconn *conn, int timeline, uint64_t durable) {
    char query[96];
    snprintf(query, sizeof(query), "TIMELINE_HISTORY %d", timeline);
    PGresult *result = PQexec(conn, query);
    if (!result || PQresultStatus(result) != PGRES_TUPLES_OK || PQntuples(result) != 1) {
        PQclear(result); return pf_fail(s, "timeline history unavailable");
    }
    const char *content = PQgetvalue(result, 0, 1);
    int old_timeline = (int)pf_number(s->state, "timeline");
    char *copy = strdup(content);
    if (!copy) { PQclear(result); return pf_fail(s, "out of memory"); }
    int valid = 0;
    char *save = NULL;
    for (char *line = strtok_r(copy, "\n", &save); line; line = strtok_r(NULL, "\n", &save)) {
        int ancestor = 0; char lsn[32] = {0}; int ok;
        if (sscanf(line, "%d %31s", &ancestor, lsn) == 2 &&
            ancestor == old_timeline && pf_parse_lsn(lsn, &ok) >= durable && ok) valid = 1;
    }
    free(copy);
    if (!valid) { PQclear(result); return pf_fail(s, "timeline fork would discard durable WAL"); }
    char id[PF_ID_SIZE];
    if (pf_upload(s, content, (size_t)PQgetlength(result, 0, 1), id)) { PQclear(result); return -1; }
    const char *existing = pf_text(s->state, "histories");
    struct json_object *histories = *existing ? json_tokener_parse(existing) : json_object_new_object();
    if (!histories || !json_object_is_type(histories, json_type_object)) {
        PQclear(result); json_object_put(histories); return pf_fail(s, "malformed timeline history manifest");
    }
    json_object_object_add(histories, PQgetvalue(result, 0, 0), json_object_new_string(id));
    pf_string(s->state, "histories", json_object_to_json_string_ext(histories, JSON_C_TO_STRING_PLAIN));
    pf_integer(s->state, "timeline", timeline);
    json_object_put(histories); PQclear(result);
    return pf_publish(s, NULL, NULL);
}

int pf_stream(pf_store *s, const char *conninfo) {
    if (pf_claim(s)) return -1;
    /* Upgrade snapshots created before the bounded-retention counter existed. */
    struct json_object *counter = NULL;
    if (!json_object_object_get_ex(s->state, "wal_records", &counter)) {
        char id[PF_ID_SIZE];
        snprintf(id, sizeof(id), "%s", pf_text(s->state, "head"));
        int64_t count = 0;
        while (*id) {
            if (++count > PF_MAX_WAL_RECORDS || strlen(id) != 32)
                return pf_fail(s, "invalid or excessive WAL chain");
            char path[128];
            snprintf(path, sizeof(path), "wal/%s", id);
            struct json_object *doc = NULL;
            if (pf_get(s, path, &doc)) return -1;
            snprintf(id, sizeof(id), "%s", pf_text(pf_fields(doc), "previous"));
            json_object_put(doc);
        }
        pf_integer(s->state, "wal_records", count);
        if (pf_publish(s, NULL, NULL)) return -1;
    }
    int ok;
    uint64_t durable = pf_parse_lsn(pf_text(s->state, "end_lsn"), &ok);
    if (!ok || !*pf_text(s->state, "base")) return pf_fail(s, "initialize a base snapshot first");
    const char *keys[] = {"dbname", "replication", "application_name", "connect_timeout", NULL};
    const char *values[] = {conninfo, "true", "pgfire", "10", NULL};
    PGconn *conn = PQconnectdbParams(keys, values, 1);
    if (!conn || PQstatus(conn) != CONNECTION_OK) {
        if (conn) PQfinish(conn);
        return pf_fail(s, "cannot connect to PostgreSQL replication endpoint");
    }
    PGresult *identity = PQexec(conn, "IDENTIFY_SYSTEM");
    if (!identity || PQresultStatus(identity) != PGRES_TUPLES_OK || PQntuples(identity) != 1 ||
        strcmp(PQgetvalue(identity, 0, 0), pf_text(s->state, "system_id"))) {
        PQclear(identity); PQfinish(conn); return pf_fail(s, "PostgreSQL system identifier mismatch");
    }
    int timeline = atoi(PQgetvalue(identity, 0, 1));
    uint64_t server_end = pf_parse_lsn(PQgetvalue(identity, 0, 2), &ok);
    PQclear(identity);
    if (!ok || server_end < durable || timeline < pf_number(s->state, "timeline")) {
        PQfinish(conn); return pf_fail(s, "PostgreSQL is behind the durable Firestore state");
    }
    if (timeline != pf_number(s->state, "timeline") && history(s, conn, timeline, durable)) {
        PQfinish(conn); return -1;
    }
    PGresult *segment = PQexec(conn, "SHOW wal_segment_size");
    if (!segment || PQresultStatus(segment) != PGRES_TUPLES_OK || PQntuples(segment) != 1 ||
        strcmp(PQgetvalue(segment, 0, 0), "16MB")) {
        PQclear(segment); PQfinish(conn); return pf_fail(s, "only 16MB WAL segments are supported");
    }
    PQclear(segment);
    char query[128], position[32];
    pf_format_lsn(durable, position);
    snprintf(query, sizeof(query), "START_REPLICATION SLOT pgfire PHYSICAL %s TIMELINE %d", position, timeline);
    PGresult *result = PQexec(conn, query);
    if (!result || PQresultStatus(result) != PGRES_COPY_BOTH) {
        PQclear(result); PQfinish(conn); return pf_fail(s, "cannot start WAL stream (missing retained WAL?)");
    }
    PQclear(result);
    fprintf(stderr, "{\"event\":\"stream_started\",\"timeline\":%d,\"durable_lsn\":\"%s\"}\n", timeline, position);
    double last_feedback = 0, last_renew = pf_monotonic();
    int rc = 0;
    while (!pf_stopping && !rc) {
        char *buffer = NULL;
        int length = PQgetCopyData(conn, &buffer, 1);
        if (length > 0) {
            unsigned char *data = (unsigned char *)buffer;
            if (data[0] == 'w' && length >= 25) {
                uint64_t start = pf_get_u64(data + 1);
                size_t payload = (size_t)length - 25;
                uint64_t end = start + payload;
                if (end < start || start > durable) rc = pf_fail(s, "WAL stream gap");
                else if (end > durable) {
                    size_t skip = (size_t)(durable - start);
                    char id[PF_ID_SIZE];
                    rc = pf_upload(s, data + 25 + skip, payload - skip, id);
                    if (!rc) {
                        struct json_object *record = json_object_new_object();
                        pf_string(record, "previous", pf_text(s->state, "head"));
                        pf_string(record, "object", id);
                        pf_integer(record, "timeline", timeline);
                        pf_format_lsn(durable, position); pf_string(record, "start", position);
                        pf_format_lsn(end, position); pf_string(record, "end", position);
                        pf_string(s->state, "head", id);
                        pf_string(s->state, "end_lsn", position);
                        rc = pf_publish(s, id, record);
                        json_object_put(record);
                        if (!rc) {
                            durable = end;
                            last_renew = pf_monotonic();
                            rc = feedback(s, conn, durable);
                            last_feedback = pf_monotonic();
                        }
                    }
                }
            } else if (data[0] == 'k' && length == 18) {
                if (data[17]) { rc = feedback(s, conn, durable); last_feedback = pf_monotonic(); }
            } else rc = pf_fail(s, "invalid replication message");
            PQfreemem(buffer);
        } else if (length < 0) {
            rc = pf_fail(s, "WAL stream ended; refusing further service");
        } else {
            struct pollfd pfd = {.fd = PQsocket(conn), .events = POLLIN};
            int ready = poll(&pfd, 1, 250);
            if (ready < 0 && errno != EINTR) rc = pf_fail(s, "replication poll failed");
            if (ready > 0 && !PQconsumeInput(conn)) rc = pf_fail(s, "replication connection lost");
        }
        if (!rc && pf_monotonic() - last_renew >= 20) {
            rc = pf_renew(s); last_renew = pf_monotonic();
        }
        if (!rc && pf_monotonic() - last_feedback >= 5) {
            rc = feedback(s, conn, durable); last_feedback = pf_monotonic();
        }
    }
    PQfinish(conn);
    /* The supervisor stops PostgreSQL before sending SIGTERM to this process. */
    if (!rc) rc = pf_release(s);
    return rc;
}

static int materialize(pf_store *s, const char *directory, int timeline, uint64_t start,
                       const unsigned char *data, size_t size) {
    while (size) {
        uint64_t segment = start / WAL_SEGMENT;
        size_t offset = (size_t)(start % WAL_SEGMENT);
        size_t length = WAL_SEGMENT - offset;
        if (length > size) length = size;
        char file[2048];
        if (snprintf(file, sizeof(file), "%s/%08X%08X%08X", directory, (unsigned int)timeline,
                     (unsigned int)(segment / 256), (unsigned int)(segment % 256)) >= (int)sizeof(file))
            return pf_fail(s, "WAL destination too long");
        FILE *output = fopen(file, "r+b");
        if (!output) {
            if (errno != ENOENT) return pf_fail(s, "cannot open WAL output");
            output = fopen(file, "w+bx");
            if (!output) return pf_fail(s, "cannot create WAL output");
            /* A new timeline can fork in the middle of an existing segment. */
            if (offset && timeline > 1) {
                char prior[2048];
                for (int ancestor = timeline - 1; ancestor >= 1; ancestor--) {
                    snprintf(prior, sizeof(prior), "%s/%08X%08X%08X", directory, (unsigned int)ancestor,
                             (unsigned int)(segment / 256), (unsigned int)(segment % 256));
                    FILE *input = fopen(prior, "rb");
                    if (!input) continue;
                    unsigned char buffer[8192];
                    size_t left = offset;
                    while (left) {
                        size_t n = left > sizeof(buffer) ? sizeof(buffer) : left;
                        if (fread(buffer, 1, n, input) != n || fwrite(buffer, 1, n, output) != n) {
                            fclose(input); fclose(output); return pf_fail(s, "timeline prefix copy failed");
                        }
                        left -= n;
                    }
                    fclose(input); break;
                }
            }
        }
        int rc = 0;
        if (ftruncate(fileno(output), WAL_SEGMENT) || fseek(output, (long)offset, SEEK_SET) ||
            fwrite(data, 1, length, output) != length) rc = pf_fail(s, "WAL materialization failed");
        if (fclose(output) && !rc) rc = pf_fail(s, "WAL close failed");
        if (rc) return rc;
        start += length; data += length; size -= length;
    }
    return 0;
}

int pf_restore(pf_store *s, const char *directory) {
    if (getenv("PGFIRE_OWNER_TOKEN") ? pf_claim(s) : pf_load(s)) return -1;
    if (!*pf_text(s->state, "base")) return pf_fail(s, "no base snapshot");
    struct stat st;
    if (stat(directory, &st) || !S_ISDIR(st.st_mode)) return pf_fail(s, "WAL destination must be an existing directory");
    struct json_object **records = NULL;
    size_t count = 0;
    char id[PF_ID_SIZE];
    snprintf(id, sizeof(id), "%s", pf_text(s->state, "head"));
    int ok;
    uint64_t expected = pf_parse_lsn(pf_text(s->state, "end_lsn"), &ok);
    int rc = ok ? 0 : pf_fail(s, "invalid durable LSN");
    while (!rc && *id) {
        if (strlen(id) != 32 || count >= PF_MAX_WAL_RECORDS) { rc = pf_fail(s, "invalid or excessive WAL chain"); break; }
        char path[128];
        snprintf(path, sizeof(path), "wal/%s", id);
        struct json_object *doc = NULL;
        if (pf_get(s, path, &doc)) { rc = -1; break; }
        struct json_object *fields = pf_fields(doc);
        uint64_t end = pf_parse_lsn(pf_text(fields, "end"), &ok);
        if (!ok || end != expected) { json_object_put(doc); rc = pf_fail(s, "broken WAL chain"); break; }
        uint64_t start = pf_parse_lsn(pf_text(fields, "start"), &ok);
        if (!ok || start >= end) { json_object_put(doc); rc = pf_fail(s, "invalid WAL range"); break; }
        struct json_object **next = realloc(records, (count + 1) * sizeof(*records));
        if (!next) { json_object_put(doc); rc = pf_fail(s, "out of memory"); break; }
        records = next; records[count++] = doc;
        expected = start;
        snprintf(id, sizeof(id), "%s", pf_text(fields, "previous"));
    }
    if (!rc) {
        uint64_t base = pf_parse_lsn(pf_text(s->state, "base_lsn"), &ok);
        if (!ok || expected != base) rc = pf_fail(s, "WAL chain does not reach base snapshot");
    }
    for (size_t i = count; i > 0 && !rc; i--) {
        struct json_object *fields = pf_fields(records[i - 1]);
        unsigned char *data = NULL; size_t size = 0;
        uint64_t start = pf_parse_lsn(pf_text(fields, "start"), &ok);
        uint64_t end = pf_parse_lsn(pf_text(fields, "end"), &ok);
        int timeline = (int)pf_number(fields, "timeline");
        if (timeline < 1 || timeline > 1000000) { rc = pf_fail(s, "invalid timeline"); break; }
        if (pf_read_object(s, pf_text(fields, "object"), &data, &size)) { rc = -1; break; }
        if ((uint64_t)size != end - start) rc = pf_fail(s, "WAL object length mismatch");
        else rc = materialize(s, directory, timeline, start, data, size);
        free(data);
    }
    for (size_t i = 0; i < count; i++) json_object_put(records[i]);
    free(records);
    if (rc) return rc;
    const char *serialized = pf_text(s->state, "histories");
    if (*serialized) {
        struct json_object *histories = json_tokener_parse(serialized);
        if (!histories || !json_object_is_type(histories, json_type_object)) {
            json_object_put(histories); return pf_fail(s, "invalid timeline history manifest");
        }
        json_object_object_foreach(histories, name, object) {
            unsigned int timeline; char tail;
            if (strlen(name) != 16 || sscanf(name, "%8X.history%c", &timeline, &tail) != 1 ||
                strstr(name, "/") || timeline == 0) { rc = pf_fail(s, "invalid timeline filename"); break; }
            char file[2048];
            snprintf(file, sizeof(file), "%s/%s", directory, name);
            if (pf_download(s, json_object_get_string(object), file)) { rc = -1; break; }
        }
        json_object_put(histories);
    }
    return rc;
}
