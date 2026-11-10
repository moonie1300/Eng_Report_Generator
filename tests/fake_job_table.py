"""In-memory conditional-update test double for the worker's DynamoDB operations."""
import threading

from botocore.exceptions import ClientError


class FakeJobTable:
    def __init__(self):
        self.items = {}
        self.statuses = []
        self.lock = threading.RLock()

    @staticmethod
    def failed():
        raise ClientError({'Error': {'Code': 'ConditionalCheckFailedException'}}, 'UpdateItem')

    def put_item(self, Item, **kwargs):
        with self.lock:
            if Item['job_id'] in self.items:
                self.failed()
            self.items[Item['job_id']] = Item.copy()

    def get_item(self, Key, **kwargs):
        with self.lock:
            item = self.items.get(Key['job_id'])
            return {'Item': item.copy()} if item else {}

    def update_item(self, Key, UpdateExpression, ConditionExpression,
                    ExpressionAttributeNames, ExpressionAttributeValues, **kwargs):
        with self.lock:
            item = self.items.get(Key['job_id'])
            values = ExpressionAttributeValues
            if not item:
                self.failed()
            if ':queued' in values:
                allowed = item['status'] == 'QUEUED' or (
                    item['status'] == 'PROCESSING' and
                    ('lease_until' not in item or item['lease_until'] <= values[':now'])
                )
            else:
                allowed = (item.get('status') == 'PROCESSING'
                           and item.get('attempt_id') == values[':owner']
                           and item.get('lease_until', 0) > values[':now'])
            if not allowed:
                self.failed()
            set_part, *remove_part = UpdateExpression.removeprefix('SET ').split(' REMOVE ')
            for assignment in set_part.split(', '):
                name, value = assignment.split(' = ')
                item[ExpressionAttributeNames.get(name, name)] = values[value]
            if remove_part:
                for name in remove_part[0].split(', '):
                    item.pop(name, None)
            if ':queued' in values or ':final' in values:
                self.statuses.append(item['status'])
            return {'Attributes': item.copy()} if kwargs.get('ReturnValues') == 'ALL_NEW' else {}
