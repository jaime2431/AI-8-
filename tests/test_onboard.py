"""The client setup script, run in dry-run mode (no Azure, Power BI or Claude calls)."""
import json

import pytest

from backbone.tenancy import ClientRegistry
from infra.onboard import main


def test_dry_run_plans_every_step_and_registers_the_client(tmp_path, capsys):
    clients = tmp_path / "clients.json"
    result = main(["--client", "ferrenort", "--name", "Ferretería Norte SRL", "--own-rnc", "131000002",
                   "--tenant", "AAAAAAAA-0000-0000-0000-00000000C11E", "--subscription", "sub-1", "--sql-admin-group-name", "Datia SQL Admins",
                   "--sql-admin-group-id", "grp-1", "--clients-file", str(clients), "--dry-run"])
    cmds = "\n".join(result["commands"])
    assert "deployment group create" in cmds and "client.bicep" in cmds
    assert "ad sp create" in cmds and cmds.count("/groups?workspaceV2=True") == 2
    assert '"identifier"' not in cmds                    # bodies are not logged
    assert "organizations/workspaces" in cmds
    assert result["secrets_set"] == ["database-url", "powerbi-client-id", "powerbi-client-secret"]
    assert "ActiveDirectoryMsi" in result["database_url"] and "Password" not in result["database_url"]
    assert "dry-run-secret" not in cmds                  # secrets never appear in commands or logs
    reg = ClientRegistry.from_file(clients)               # the new entry is a valid client configuration
    c = reg.get("ferrenort")
    assert c.entra_tenant_id == "aaaaaaaa-0000-0000-0000-00000000c11e" and c.powerbi.workspace_id.endswith("11fe")
    assert "dry run" in capsys.readouterr().out


def test_bad_slug_refused(tmp_path):
    with pytest.raises(SystemExit):
        main(["--client", "Ferreteria-Norte", "--name", "x", "--own-rnc", "1", "--tenant", "t", "--subscription", "s",
              "--sql-admin-group-name", "g", "--sql-admin-group-id", "g", "--dry-run"])


def test_tenant_already_used_by_another_client_is_refused(tmp_path):
    clients = tmp_path / "clients.json"
    args = ["--name", "X", "--own-rnc", "131000002", "--tenant", "AAAAAAAA-0000-0000-0000-00000000C11E",
            "--subscription", "s", "--sql-admin-group-name", "g", "--sql-admin-group-id", "g",
            "--clients-file", str(clients), "--dry-run"]
    main(["--client", "first"] + args)
    main(["--client", "first"] + args)                    # re-running the same client is fine
    with pytest.raises(SystemExit, match="already belongs"):
        main(["--client", "second"] + args)
    with pytest.raises(SystemExit, match="GUID"):
        main(["--client", "third"] + [x if x != "AAAAAAAA-0000-0000-0000-00000000C11E" else "not-a-guid" for x in args])
