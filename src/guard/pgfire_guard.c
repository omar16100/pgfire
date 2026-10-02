/*
 * Guard the durability contract around PostgreSQL's synchronous replication.
 * In particular, PostgreSQL normally allows canceling a synchronous wait while
 * still returning COMMIT success. pgfire must close that connection instead.
 */
#include "postgres.h"
#include "fmgr.h"
#include "access/xact.h"
#include "access/xlog.h"
#include "catalog/pg_class.h"
#include "executor/executor.h"
#include "miscadmin.h"
#include "nodes/parsenodes.h"
#include "replication/slot.h"
#include "storage/lwlock.h"
#include "tcop/dest.h"
#include "tcop/tcopprot.h"
#include "tcop/utility.h"
#include "utils/errcodes.h"

PG_MODULE_MAGIC;
void _PG_init(void);

static ProcessUtility_hook_type previous_utility;
static ExecutorEnd_hook_type previous_end;

static void require_sync(int severity)
{
    /* Maintenance transactions have no application COMMIT acknowledgement. */
    if (MyBackendType != B_BACKEND) return;
    if (synchronous_commit != SYNCHRONOUS_COMMIT_REMOTE_FLUSH)
        ereport(severity, (errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
                          errmsg("pgfire requires synchronous_commit=on")));
}

static void guard_end(QueryDesc *query)
{
    require_sync(ERROR);
    if (previous_end) previous_end(query);
    else standard_ExecutorEnd(query);
}

static void guard_utility(PlannedStmt *plan, const char *query, bool read_only,
                          ProcessUtilityContext context, ParamListInfo params,
                          QueryEnvironment *environment, DestReceiver *dest,
                          QueryCompletion *completion)
{
    Node *node = plan->utilityStmt;
    require_sync(ERROR);
    if (IsA(node, CreateStmt) && ((CreateStmt *)node)->relation->relpersistence == RELPERSISTENCE_UNLOGGED)
        ereport(ERROR, (errcode(ERRCODE_FEATURE_NOT_SUPPORTED), errmsg("pgfire does not support unlogged tables")));
    if (IsA(node, CreateTableAsStmt) && ((CreateTableAsStmt *)node)->into->rel->relpersistence == RELPERSISTENCE_UNLOGGED)
        ereport(ERROR, (errcode(ERRCODE_FEATURE_NOT_SUPPORTED), errmsg("pgfire does not support unlogged tables")));
    if (IsA(node, AlterTableStmt))
    {
        ListCell *cell;
        foreach(cell, ((AlterTableStmt *)node)->cmds)
            if (((AlterTableCmd *)lfirst(cell))->subtype == AT_SetUnLogged)
                ereport(ERROR, (errcode(ERRCODE_FEATURE_NOT_SUPPORTED), errmsg("pgfire does not support unlogged tables")));
    }
    if (IsA(node, TransactionStmt) && ((TransactionStmt *)node)->kind == TRANS_STMT_PREPARE)
        ereport(ERROR, (errcode(ERRCODE_FEATURE_NOT_SUPPORTED), errmsg("pgfire does not support prepared transactions")));
    if (previous_utility) previous_utility(plan, query, read_only, context, params, environment, dest, completion);
    else standard_ProcessUtility(plan, query, read_only, context, params, environment, dest, completion);
    require_sync(ERROR);
}

static XLogRecPtr durable_position(void)
{
    XLogRecPtr position = InvalidXLogRecPtr;
    int i;
    LWLockAcquire(ReplicationSlotControlLock, LW_SHARED);
    for (i = 0; i < max_replication_slots; i++)
    {
        ReplicationSlot *slot = &ReplicationSlotCtl->replication_slots[i];
        if (slot->in_use && strcmp(NameStr(slot->data.name), "pgfire") == 0)
        {
            SpinLockAcquire(&slot->mutex);
            position = slot->data.restart_lsn;
            SpinLockRelease(&slot->mutex);
            break;
        }
    }
    LWLockRelease(ReplicationSlotControlLock);
    return position;
}

static void guard_commit(XactEvent event, void *argument)
{
    int attempts;
    (void) argument;
    if (event != XACT_EVENT_COMMIT || MyBackendType != B_BACKEND ||
        XactLastCommitEnd == InvalidXLogRecPtr) return;
    /* Slot advancement follows the normal synchronous wake-up in walsender. */
    for (attempts = 0; attempts < 500; attempts++)
    {
        if (synchronous_commit == SYNCHRONOUS_COMMIT_REMOTE_FLUSH &&
            durable_position() >= XactLastCommitEnd) return;
        if (QueryCancelPending || ProcDiePending) break;
        pg_usleep(10000L);
    }
    /*
     * Do not raise ERROR/FATAL from a committed transaction: trying to abort it
     * causes PANIC. Follow SyncRepWaitForLSN's administrator-shutdown path:
     * suppress the success response and terminate after transaction cleanup.
     */
    ereport(WARNING, (errcode(ERRCODE_CONNECTION_FAILURE),
                      errmsg("pgfire commit outcome is unknown: WAL was not confirmed durable"),
                      errhint("Reconnect and check the transaction outcome before retrying.")));
    whereToSendOutput = DestNone;
    ProcDiePending = true;
    InterruptPending = true;
}

void _PG_init(void)
{
    if (!process_shared_preload_libraries_in_progress)
        ereport(ERROR, (errmsg("pgfire_guard must be loaded through shared_preload_libraries")));
    previous_utility = ProcessUtility_hook;
    ProcessUtility_hook = guard_utility;
    previous_end = ExecutorEnd_hook;
    ExecutorEnd_hook = guard_end;
    RegisterXactCallback(guard_commit, NULL);
}
