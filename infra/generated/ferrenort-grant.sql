-- Run in the 'clean' database of sql-dba-ferrenort-x.database.windows.net (Azure portal > Query editor, signed in as a member of the SQL admin group)
CREATE USER [id-dba-ferrenort] FROM EXTERNAL PROVIDER;
ALTER ROLE db_datareader ADD MEMBER [id-dba-ferrenort];
ALTER ROLE db_datawriter ADD MEMBER [id-dba-ferrenort];
ALTER ROLE db_ddladmin ADD MEMBER [id-dba-ferrenort];
