from balaji import storage, tools


def test_notes_persist_and_search():
    assert 'Saved memory' in tools.remember_fact('I like Python')
    assert 'Python' in tools.search_memory('Python')
    assert 'No matching' in tools.search_memory('Rust')


def test_secret_memory_rejected():
    assert 'Refused' in tools.remember_fact('my password is 123')
    assert storage.notes() == []


def test_tasks_create_and_complete():
    assert 'Created task #1' in tools.create_task('Revise LangGraph')
    assert 'LangGraph' in tools.list_tasks()
    assert 'Completed task #1' in tools.complete_task(1)
    assert 'No pending' in tools.list_tasks()
    assert storage.tasks(include_done=True)[0]['completed'] == 1


def test_no_sql_injection():
    tools.create_task("Hi'); DROP TABLE tasks;--")
    assert len(storage.tasks()) == 1


def test_session_history_isolated():
    storage.add_message('a','user','secret for session a')
    storage.add_message('b','user','hello from b')
    assert len(storage.history('a')) == 1
    assert 'secret' not in str(storage.history('b'))
    storage.clear_history('a')
    assert storage.history('a') == []
    assert len(storage.history('b')) == 1


def test_tool_rejects_unexpected_argument():
    assert tools.execute_tool('create_task', {'description':'x','shell':'rm -rf /'}) .startswith('Rejected')
    assert storage.tasks() == []


def test_tool_allowlist():
    assert 'Rejected' in tools.execute_tool('run_shell', {'cmd': 'whoami'})
