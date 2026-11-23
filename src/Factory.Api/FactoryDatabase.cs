using System.Data.Common;
using Microsoft.Data.Sqlite;
using Npgsql;

namespace Factory;

public sealed class FactoryDatabase
{
    private readonly Func<DbConnection> connect;
    private readonly bool postgres;

    public FactoryDatabase(string directory, string? postgresConnection)
    {
        postgres = !string.IsNullOrWhiteSpace(postgresConnection);
        if (postgres) connect = () => new NpgsqlConnection(postgresConnection);
        else
        {
            Directory.CreateDirectory(directory);
            var settings = new SqliteConnectionStringBuilder { DataSource = Path.Combine(directory, "slice.db"), ForeignKeys = true };
            connect = () => new SqliteConnection(settings.ToString());
            using var db = Open();
            using var wal = db.CreateCommand();
            wal.CommandText = "PRAGMA journal_mode=WAL";
            wal.ExecuteNonQuery();
        }
        using var resource = typeof(FactoryDatabase).Assembly.GetManifestResourceStream("001_dispatch.sql")!;
        using var reader = new StreamReader(resource);
        Write(s => s.Execute(reader.ReadToEnd()));
    }

    public DbConnection Open()
    {
        var db = connect();
        db.Open();
        return db;
    }

    public T Read<T>(Func<Session,T> query)
    {
        using var db = Open();
        using var tx = db is SqliteConnection sqlite ? sqlite.BeginTransaction(deferred: true)
            : db.BeginTransaction(System.Data.IsolationLevel.RepeatableRead);
        var result = query(new Session(db, tx));
        tx.Commit();
        return result;
    }

    public T Write<T>(Func<Session,T> change)
    {
        using var db = Open();
        using var tx = db is SqliteConnection sqlite ? sqlite.BeginTransaction(deferred:false) : db.BeginTransaction();
        var session = new Session(db, tx);
        // One active coding pipeline. Serialize short admission/receipt decisions across controllers.
        if (postgres) session.Execute("SELECT pg_advisory_xact_lock(813774401)");
        var result = change(session);
        tx.Commit();
        return result;
    }
}

public sealed class Session(DbConnection db, DbTransaction? tx)
{
    public DbCommand Command(string sql, params (string Name, object Value)[] parameters)
    {
        var cmd = db.CreateCommand();
        cmd.Transaction = tx;
        cmd.CommandText = sql;
        foreach (var (name, value) in parameters)
        {
            var parameter = cmd.CreateParameter();
            parameter.ParameterName = name;
            parameter.Value = value;
            cmd.Parameters.Add(parameter);
        }
        return cmd;
    }

    public string? Text(string sql, params (string, object)[] parameters)
    {
        using var cmd = Command(sql, parameters);
        return cmd.ExecuteScalar() as string;
    }

    public int Execute(string sql, params (string, object)[] parameters)
    {
        using var cmd = Command(sql, parameters);
        return cmd.ExecuteNonQuery();
    }
}
