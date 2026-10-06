-- Run in the 'clean' database of sql-datia-first-x.database.windows.net (Azure portal > Query editor, signed in as a member of the SQL admin group)
CREATE USER [id-datia-first] FROM EXTERNAL PROVIDER;
ALTER ROLE db_datareader ADD MEMBER [id-datia-first];
ALTER ROLE db_datawriter ADD MEMBER [id-datia-first];
ALTER ROLE db_ddladmin ADD MEMBER [id-datia-first];
