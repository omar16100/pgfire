#ifndef PGFIRE_H
#define PGFIRE_H
#include <stddef.h>
#include <stdint.h>
#include <signal.h>
#include <json-c/json.h>
#define PF_CHUNK (512u * 1024u)
#define PF_LEASE_SECONDS 90
#define PF_ID_SIZE 33
#define PF_MAX_WAL_RECORDS 100000
typedef struct {
    char endpoint[512], root[1024], owner[PF_ID_SIZE], version[96], error[512];
    struct json_object *state;
    void *http;
    double deadline;
    int held;
    long http_status;
} pf_store;
extern volatile sig_atomic_t pf_stopping;
int pf_open(pf_store *s);
void pf_close(pf_store *s);
int pf_load(pf_store *s);
int pf_claim(pf_store *s);
int pf_renew(pf_store *s);
int pf_release(pf_store *s);
int pf_publish(pf_store *s, const char *record_id, struct json_object *record);
int pf_get(pf_store *s, const char *path, struct json_object **result);
int pf_put_immutable(pf_store *s, const char *path, struct json_object *fields);
int pf_upload(pf_store *s, const void *data, size_t size, char id[PF_ID_SIZE]);
int pf_upload_file(pf_store *s, const char *file, char id[PF_ID_SIZE]);
int pf_download(pf_store *s, const char *id, const char *file);
int pf_read_object(pf_store *s, const char *id, unsigned char **data, size_t *size);
int pf_stream(pf_store *s, const char *conninfo);
int pf_restore(pf_store *s, const char *destination);
uint64_t pf_parse_lsn(const char *text, int *ok);
void pf_format_lsn(uint64_t lsn, char output[32]);
uint64_t pf_get_u64(const unsigned char *data);
void pf_set_u64(unsigned char *data, uint64_t value);
void pf_string(struct json_object *fields, const char *key, const char *value);
void pf_integer(struct json_object *fields, const char *key, int64_t value);
const char *pf_text(struct json_object *fields, const char *key);
int64_t pf_number(struct json_object *fields, const char *key);
struct json_object *pf_fields(struct json_object *document);
double pf_monotonic(void);
int pf_fail(pf_store *s, const char *format, ...);
#endif
